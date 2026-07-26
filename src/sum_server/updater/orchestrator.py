"""Server self-update state machine.

Pure orchestration over an injected :class:`Runner` (real shell-out) and
:class:`Reporter` (status persistence), so every path — including each
failure→rollback branch — is unit-testable without touching git/systemd/pg.
Async throughout, matching the rest of the codebase.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Protocol


class Runner(Protocol):
    """Executes the real update steps. Each method raises on failure."""

    async def git_dirty_paths(self) -> str: ...
    async def current_git_ref(self) -> str: ...
    async def pg_dump(self, dest: Path) -> None: ...
    async def git_fetch(self) -> None: ...
    async def git_checkout(self, ref: str) -> None: ...
    async def uv_sync(self) -> None: ...
    async def alembic_upgrade(self) -> None: ...
    async def restart_service(self) -> None: ...
    async def wait_healthy(self, expected_version: str) -> bool: ...
    async def pg_restore(self, dump: Path) -> None: ...


class Reporter(Protocol):
    """Persists status transitions (durable across the sum-server restart)."""

    async def set(self, status: str, detail: str | None = None) -> None: ...
    async def set_dump_path(self, path: str) -> None: ...


async def run_update(
    *,
    target_version: str,
    current_version: str,
    runner: Runner,
    report: Reporter,
    dumps_dir: Path,
) -> str:
    """Drive one update attempt. Returns a terminal status.

    ``success`` — running ``target_version`` and healthy.
    ``rolled_back`` — update failed; restored to ``current_version``.
    ``failed`` — could not complete *or* could not roll back (dump preserved).
    """
    # Name the offending paths: the operator otherwise has to go and look, and
    # the usual culprit (a lockfile rewritten by a non-frozen uv run) is not
    # something they would guess.
    dirty = await runner.git_dirty_paths()
    if dirty:
        listed = ", ".join(dirty.split("\n")[:5])
        await report.set("failed", f"working tree is dirty; refusing to update: {listed}")
        return "failed"

    old_ref = await runner.current_git_ref()

    await report.set("snapshotting")
    dumps_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - one-shot updater, not latency-sensitive
    stamp = dt.datetime.now(tz=dt.UTC).strftime("%Y%m%d-%H%M%S")
    dump = dumps_dir / f"pre-{target_version}-{stamp}.dump"
    try:
        await runner.pg_dump(dump)
    except Exception as exc:
        # Nothing changed yet; safe to just fail.
        await report.set("failed", f"database snapshot failed: {exc}")
        return "failed"
    await report.set_dump_path(str(dump))

    try:
        await report.set("checking_out")
        await runner.git_fetch()
        await runner.git_checkout(f"v{target_version}")
        await report.set("syncing")
        await runner.uv_sync()
        await report.set("migrating")
        await runner.alembic_upgrade()
        await report.set("restarting")
        await runner.restart_service()
        await report.set("verifying")
        if await runner.wait_healthy(target_version):
            await report.set("success", f"updated to {target_version}")
            return "success"
        reason = "health check failed after update"
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"

    return await _rollback(
        old_ref=old_ref,
        dump=dump,
        current_version=current_version,
        runner=runner,
        report=report,
        reason=reason,
    )


async def _rollback(
    *,
    old_ref: str,
    dump: Path,
    current_version: str,
    runner: Runner,
    report: Reporter,
    reason: str,
) -> str:
    await report.set("rolling_back", reason)
    try:
        await runner.git_checkout(old_ref)
        await runner.uv_sync()
        await runner.pg_restore(dump)
        await runner.restart_service()
        if await runner.wait_healthy(current_version):
            await report.set("rolled_back", f"rolled back to {current_version} ({reason})")
            return "rolled_back"
        await report.set(
            "failed",
            f"rollback health check failed; restore manually from {dump} ({reason})",
        )
        return "failed"
    except Exception as exc:
        await report.set("failed", f"rollback failed: {exc}; dump at {dump} ({reason})")
        return "failed"
