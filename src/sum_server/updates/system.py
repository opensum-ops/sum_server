"""Server self-update: availability gate, request/guard, and launcher.

The endpoint validates and queues a ``server_updates`` row, then launches the
out-of-process updater via ``systemd-run`` (its own transient unit, so the
sum-server restart the update triggers can't kill it).

Two things about that transient unit drive the code below:

- **It does not inherit our environment.** A deployment that configures
  ``SUM_SERVER_*`` through systemd ``Environment=``/``EnvironmentFile=`` would
  hand the updater nothing, and it would die building its ``Settings`` before it
  could report anything. So we forward the effective settings explicitly, via a
  root-only ``EnvironmentFile`` rather than ``--setenv`` (which would put the
  database password in the unit's properties and in ``ps`` output).
- **Its PATH is the systemd default**, which does not include ``~/.local/bin``
  where ``uv`` is commonly installed. ``uv`` is resolved to an absolute path here
  and checked before the update is offered, so a missing ``uv`` is a refusal up
  front instead of a mid-update rollback.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import shutil
import uuid
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sum_server import __version__
from sum_server.core.audit import write_audit
from sum_server.core.context import Actor
from sum_server.core.errors import ConflictError
from sum_server.core.ids import new_id
from sum_server.core.versions import is_newer
from sum_server.settings import get_settings
from sum_server.updates.models import ServerUpdate, is_terminal

log = structlog.get_logger(__name__)

# Places uv commonly lives when it is not on the systemd unit's PATH.
_UV_FALLBACKS = ("/usr/local/bin/uv", "/usr/bin/uv", "/root/.local/bin/uv")

# A queued row whose updater never checked in (never set started_at) is treated
# as abandoned after this long, so one failed launch cannot wedge the feature.
ABANDONED_QUEUED_SECONDS = 120
# A run that started but stopped reporting is abandoned after this long. The
# real path (dump, checkout, sync, migrate, restart, verify) is minutes, and
# ShellRunner caps each command at 1800s.
ABANDONED_RUNNING_SECONDS = 3600


def resolve_uv() -> str | None:
    """Absolute path to ``uv``, or ``None`` if it cannot be found.

    Order: the explicit ``SUM_SERVER_UV_BIN`` setting, then ``PATH``, then the
    usual install locations. Resolved in the *server* process and forwarded to
    the updater, because the transient unit's PATH is narrower than ours.
    """
    configured = get_settings().uv_bin.strip()
    if configured:
        return configured if Path(configured).exists() else None
    found = shutil.which("uv")
    if found:
        return found
    return next((p for p in _UV_FALLBACKS if Path(p).exists()), None)


def self_update_available() -> tuple[bool, str]:
    """Whether this deployment can self-update, and why not if it can't."""
    settings = get_settings()
    if not settings.install_dir:
        return False, "SUM_SERVER_INSTALL_DIR is not set"
    if shutil.which("systemd-run") is None:
        return False, "systemd-run not available"
    if os.geteuid() != 0:
        return False, "server is not running as root"
    if resolve_uv() is None:
        return False, "uv not found on PATH; set SUM_SERVER_UV_BIN to its absolute path"
    return True, ""


def _updater_env() -> dict[str, str]:
    """The settings the updater needs, as ``SUM_SERVER_*`` variables.

    Taken from *our* effective settings, so it does not matter whether they
    reached us via the environment, an env file, or defaults.
    """
    settings = get_settings()
    env = {
        "SUM_SERVER_DATABASE_URL": settings.database_url,
        "SUM_SERVER_SIGNING_PRIVATE_KEY": settings.signing_private_key,
        "SUM_SERVER_INSTALL_DIR": settings.install_dir,
        "SUM_SERVER_DATA_DIR": str(settings.data_dir),
        "SUM_SERVER_SERVICE_NAME": settings.service_name,
        "SUM_SERVER_EXTERNAL_URL": settings.external_url,
        "SUM_SERVER_ENV": settings.env.value,
        "SUM_SERVER_LOG_LEVEL": settings.log_level,
        "SUM_SERVER_LOG_FORMAT": settings.log_format.value,
        # Never let the updater's own process start a release-check loop.
        "SUM_SERVER_UPDATE_CHECK_ENABLED": "false",
    }
    uv = resolve_uv()
    if uv:
        env["SUM_SERVER_UV_BIN"] = uv
    return {k: v for k, v in env.items() if v != ""}


def _systemd_escape(value: str) -> str:
    """Quote a value for a systemd ``EnvironmentFile`` line."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def write_updater_env_file() -> Path:
    """Write the updater's settings to a root-only file and return its path.

    Deliberately under ``data_dir``, never inside ``install_dir``: a stray file
    in the git checkout would make the working tree dirty, and the updater
    refuses to run against a dirty tree.
    """
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    path = settings.data_dir / "updater.env"
    body = "".join(f"{k}={_systemd_escape(v)}\n" for k, v in _updater_env().items())
    path.write_text(body, encoding="utf-8")
    path.chmod(0o600)
    return path


async def latest_update(session: AsyncSession) -> ServerUpdate | None:
    return (
        await session.execute(
            select(ServerUpdate).order_by(ServerUpdate.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()


def is_abandoned(row: ServerUpdate, *, now: dt.datetime | None = None) -> bool:
    """True if a non-terminal row's updater is never going to report again.

    The updater's first database write is ``started_at``. A row that never got
    one has an updater that died before it could reach the database (a bad
    launch, or settings it could not build). Without this, a single such failure
    would block every future update behind the concurrency guard.
    """
    if is_terminal(row.status):
        return False
    now = now or dt.datetime.now(tz=dt.UTC)
    if row.started_at is None:
        return (now - row.created_at).total_seconds() > ABANDONED_QUEUED_SECONDS
    return (now - row.started_at).total_seconds() > ABANDONED_RUNNING_SECONDS


async def _reap_if_abandoned(session: AsyncSession, row: ServerUpdate) -> bool:
    """Mark an abandoned row failed. Returns whether it was reaped."""
    if not is_abandoned(row):
        return False
    row.status = "failed"
    row.detail = "updater stopped reporting; marked failed so updates are not blocked"
    row.finished_at = dt.datetime.now(tz=dt.UTC)
    await write_audit(
        session,
        action="system.update_abandoned",
        target_kind="system",
        target_id=None,
        payload={"to": row.to_version, "was": row.status},
    )
    log.warning("update_abandoned", to=row.to_version)
    return True


async def fail_update_by_id(session: AsyncSession, row_id: uuid.UUID, detail: str) -> None:
    """Terminate a row we know will never run (e.g. the launch itself failed).

    Opens its own transaction: the caller has already committed the queued row,
    which is what made it visible to an updater that then never started.
    """
    async with session.begin():
        row = (
            await session.execute(select(ServerUpdate).where(ServerUpdate.id == row_id))
        ).scalar_one_or_none()
        if row is None or is_terminal(row.status):
            return
        row.status = "failed"
        row.detail = detail[:2048]
        row.finished_at = dt.datetime.now(tz=dt.UTC)
        await write_audit(
            session,
            action="system.update_failed",
            target_kind="system",
            target_id=None,
            payload={"to": row.to_version, "detail": detail[:512]},
        )
    log.warning("update_launch_failed", detail=detail[:200])


async def request_server_update(
    session: AsyncSession, *, target_version: str, actor: Actor
) -> ServerUpdate:
    ok, reason = self_update_available()
    if not ok:
        raise ConflictError(f"server self-update unavailable: {reason}")
    if not is_newer(target_version, __version__):
        raise ConflictError(f"target {target_version} is not newer than {__version__}")

    # A live run blocks a new one; an abandoned row is reaped and does not.
    existing = await latest_update(session)
    if (
        existing is not None
        and not is_terminal(existing.status)
        and not await _reap_if_abandoned(session, existing)
    ):
        raise ConflictError(f"an update is already in progress ({existing.status})")

    row = ServerUpdate(
        id=new_id(),
        from_version=__version__,
        to_version=target_version,
        status="queued",
        requested_by=actor.id,
    )
    session.add(row)
    await session.flush()
    await write_audit(
        session,
        action="system.update_requested",
        target_kind="system",
        target_id=None,
        payload={"from": __version__, "to": target_version},
        actor_kind=actor.kind,
        actor_id=actor.id,
    )
    return row


async def launch_updater() -> None:
    """Start the updater in its own transient systemd unit (survives restart).

    The unit gets an explicit ``EnvironmentFile``: it inherits nothing from us,
    so without one the updater cannot even build its settings.
    """
    settings = get_settings()
    python = f"{settings.install_dir}/.venv/bin/python"
    env_file = write_updater_env_file()
    proc = await asyncio.create_subprocess_exec(
        "systemd-run",
        "--collect",
        f"--unit=sum-server-update-{new_id().hex[:8]}",
        f"--working-directory={settings.install_dir}",
        "--property",
        f"EnvironmentFile={env_file}",
        python,
        "-m",
        "sum_server.updater",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _out, err = await proc.communicate()
    if proc.returncode != 0:
        raise ConflictError(f"could not launch updater: {err.decode()[-300:]}")
    log.info("updater_launched", env_file=str(env_file))
