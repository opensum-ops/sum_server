"""Real async :class:`Runner`: git / uv / alembic / systemctl / pg + health poll."""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import httpx
import structlog

log = structlog.get_logger(__name__)


class CommandError(RuntimeError):
    pass


class ShellRunner:
    def __init__(
        self,
        *,
        install_dir: Path,
        service_name: str,
        database_url: str,
        health_url: str,
        health_timeout_seconds: int = 120,
        uv_bin: str = "",
    ) -> None:
        self._dir = install_dir
        self._service = service_name
        self._database_url = database_url
        self._health_url = health_url.rstrip("/")
        self._health_timeout = health_timeout_seconds
        self._alembic = str(install_dir / ".venv" / "bin" / "alembic")
        # Absolute, because our transient unit's PATH is the systemd default and
        # will not find a uv installed under ~/.local/bin.
        self._uv = uv_bin or "uv"

    async def _run(self, *args: str) -> str:
        log.info("updater_exec", cmd=" ".join(args))
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(self._dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=1800)
        if proc.returncode != 0:
            raise CommandError(f"{args[0]} failed ({proc.returncode}): {err.decode()[-500:]}")
        return out.decode()

    # --- Runner protocol ---------------------------------------------------

    async def git_is_dirty(self) -> bool:
        return bool((await self._run("git", "status", "--porcelain")).strip())

    async def current_git_ref(self) -> str:
        return (await self._run("git", "rev-parse", "HEAD")).strip()

    async def pg_dump(self, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        await self._run(
            "pg_dump", "-Fc", "--dbname", _libpq_url(self._database_url), "-f", str(dest)
        )

    async def git_fetch(self) -> None:
        await self._run("git", "fetch", "--tags", "--force")

    async def git_checkout(self, ref: str) -> None:
        await self._run("git", "checkout", "--force", ref)

    async def uv_sync(self) -> None:
        await self._run(self._uv, "sync", "--frozen")

    async def alembic_upgrade(self) -> None:
        await self._run(self._alembic, "upgrade", "head")

    async def restart_service(self) -> None:
        await self._run("systemctl", "restart", self._service)

    async def pg_restore(self, dump: Path) -> None:
        await self._run(
            "pg_restore",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--dbname",
            _libpq_url(self._database_url),
            str(dump),
        )

    async def wait_healthy(self, expected_version: str) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._health_timeout
        async with httpx.AsyncClient(timeout=5.0, verify=False) as client:  # noqa: S501
            while loop.time() < deadline:
                try:
                    resp = await client.get(f"{self._health_url}/readyz")
                    body = resp.json()
                    if resp.status_code == 200 and body.get("version") == expected_version:
                        return True
                except (httpx.HTTPError, ValueError):
                    pass
                await asyncio.sleep(3)
        return False


def _libpq_url(async_url: str) -> str:
    """Turn ``postgresql+asyncpg://...`` into a libpq URL for pg_dump/restore."""
    parsed = urlparse(async_url)
    return urlunparse(parsed._replace(scheme=parsed.scheme.split("+", 1)[0]))
