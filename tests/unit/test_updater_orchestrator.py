"""Server self-update state machine: every path with a scripted fake runner."""

from __future__ import annotations

from pathlib import Path

import pytest

from sum_server.updater.orchestrator import run_update


class FakeRunner:
    """Records calls; each step can be told to raise, and health results scripted."""

    def __init__(
        self,
        *,
        dirty: bool = False,
        fail_at: str | None = None,
        health_results: list[bool] | None = None,
    ) -> None:
        self.dirty = dirty
        self.fail_at = fail_at
        self.health_results = health_results if health_results is not None else [True]
        self.calls: list[str] = []
        self._failed = False

    def _maybe_fail(self, name: str) -> None:
        self.calls.append(name)
        # Fail only the first time (so a step reused during rollback succeeds).
        if name == self.fail_at and not self._failed:
            self._failed = True
            raise RuntimeError(f"{name} boom")

    async def git_dirty_paths(self) -> str:
        return " M uv.lock\n?? stray.txt" if self.dirty else ""

    async def current_git_ref(self) -> str:
        return "oldsha"

    async def pg_dump(self, dest: Path) -> None:
        self._maybe_fail("pg_dump")

    async def git_fetch(self) -> None:
        self._maybe_fail("git_fetch")

    async def git_checkout(self, ref: str) -> None:
        self._maybe_fail(f"git_checkout:{ref}")

    async def uv_sync(self) -> None:
        self._maybe_fail("uv_sync")

    async def alembic_upgrade(self) -> None:
        self._maybe_fail("alembic_upgrade")

    async def restart_service(self) -> None:
        self._maybe_fail("restart_service")

    async def wait_healthy(self, expected_version: str) -> bool:
        self.calls.append(f"health:{expected_version}")
        return self.health_results.pop(0) if self.health_results else False

    async def pg_restore(self, dump: Path) -> None:
        self._maybe_fail("pg_restore")


class Recorder:
    def __init__(self) -> None:
        self.transitions: list[str] = []
        self.details: list[str] = []
        self.dump_path: str | None = None

    async def set(self, status: str, detail: str | None = None) -> None:
        self.transitions.append(status)
        self.details.append(detail or "")

    async def set_dump_path(self, path: str) -> None:
        self.dump_path = path


async def _run(runner: FakeRunner, tmp_path: Path) -> tuple[str, Recorder]:
    rec = Recorder()
    result = await run_update(
        target_version="0.3.0",
        current_version="0.2.0",
        runner=runner,
        report=rec,
        dumps_dir=tmp_path,
    )
    return result, rec


async def test_happy_path(tmp_path: Path) -> None:
    result, rec = await _run(FakeRunner(health_results=[True]), tmp_path)
    assert result == "success"
    assert rec.transitions[-1] == "success"
    assert "checking_out" in rec.transitions
    assert "migrating" in rec.transitions
    assert rec.dump_path is not None


async def test_dirty_tree_refused(tmp_path: Path) -> None:
    result, rec = await _run(FakeRunner(dirty=True), tmp_path)
    assert result == "failed"
    assert rec.transitions == ["failed"]
    # Name the offending paths: an operator reading only the status panel
    # should not have to go and look for them.
    assert "uv.lock" in rec.details[-1]
    assert "stray.txt" in rec.details[-1]


async def test_dump_failure_no_rollback(tmp_path: Path) -> None:
    result, rec = await _run(FakeRunner(fail_at="pg_dump"), tmp_path)
    assert result == "failed"
    assert "rolling_back" not in rec.transitions  # nothing changed yet


@pytest.mark.parametrize("fail_at", ["git_checkout:v0.3.0", "uv_sync", "alembic_upgrade"])
async def test_step_failure_rolls_back(tmp_path: Path, fail_at: str) -> None:
    runner = FakeRunner(fail_at=fail_at, health_results=[True])  # rollback health ok
    result, rec = await _run(runner, tmp_path)
    assert result == "rolled_back"
    assert "rolling_back" in rec.transitions
    # rollback checks out the old ref and restores the dump
    assert "git_checkout:oldsha" in runner.calls
    assert "pg_restore" in runner.calls


async def test_unhealthy_after_update_rolls_back(tmp_path: Path) -> None:
    # First health (post-update) fails; second (post-rollback) succeeds.
    runner = FakeRunner(health_results=[False, True])
    result, _rec = await _run(runner, tmp_path)
    assert result == "rolled_back"
    assert "pg_restore" in runner.calls


async def test_failed_rollback_is_failed(tmp_path: Path) -> None:
    # Update health fails, and rollback restore also fails -> terminal failed.
    runner = FakeRunner(fail_at="pg_restore", health_results=[False])
    result, rec = await _run(runner, tmp_path)
    assert result == "failed"
    assert rec.transitions[-1] == "failed"
