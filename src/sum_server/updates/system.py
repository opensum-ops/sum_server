"""Server self-update: availability gate, request/guard, and launcher.

The endpoint validates and queues a ``server_updates`` row, then launches the
out-of-process updater via ``systemd-run`` (its own transient unit, so the
sum-server restart the update triggers can't kill it).
"""

from __future__ import annotations

import asyncio
import os
import shutil

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


def self_update_available() -> tuple[bool, str]:
    """Whether this deployment can self-update, and why not if it can't."""
    settings = get_settings()
    if not settings.install_dir:
        return False, "SUM_SERVER_INSTALL_DIR is not set"
    if shutil.which("systemd-run") is None:
        return False, "systemd-run not available"
    if os.geteuid() != 0:
        return False, "server is not running as root"
    return True, ""


async def latest_update(session: AsyncSession) -> ServerUpdate | None:
    return (
        await session.execute(
            select(ServerUpdate).order_by(ServerUpdate.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()


async def request_server_update(
    session: AsyncSession, *, target_version: str, actor: Actor
) -> ServerUpdate:
    ok, reason = self_update_available()
    if not ok:
        raise ConflictError(f"server self-update unavailable: {reason}")
    if not is_newer(target_version, __version__):
        raise ConflictError(f"target {target_version} is not newer than {__version__}")

    existing = await latest_update(session)
    if existing is not None and not is_terminal(existing.status):
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
    """Start the updater in its own transient systemd unit (survives restart)."""
    settings = get_settings()
    python = f"{settings.install_dir}/.venv/bin/python"
    proc = await asyncio.create_subprocess_exec(
        "systemd-run",
        "--collect",
        f"--unit=sum-server-update-{new_id().hex[:8]}",
        f"--working-directory={settings.install_dir}",
        python,
        "-m",
        "sum_server.updater",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _out, err = await proc.communicate()
    if proc.returncode != 0:
        raise ConflictError(f"could not launch updater: {err.decode()[-300:]}")
    log.info("updater_launched")
