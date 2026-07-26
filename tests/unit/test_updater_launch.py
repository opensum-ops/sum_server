"""Updater launch hardening.

The transient systemd unit inherits neither our environment nor a PATH wide
enough to find uv, and an updater that dies before its first database write
used to leave a non-terminal row that blocked every later update. These pin
each of those down.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from sum_server.settings import get_settings
from sum_server.updates import system as system_svc
from sum_server.updates.models import ServerUpdate


@pytest.fixture(autouse=True)
def _fresh_settings() -> None:
    get_settings.cache_clear()


def _reload_settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


# --- settings forwarding ----------------------------------------------------


def test_updater_env_carries_what_settings_requires(monkeypatch: pytest.MonkeyPatch) -> None:
    """The updater builds Settings first thing; the required fields must reach it."""
    _reload_settings(monkeypatch, SUM_SERVER_INSTALL_DIR="/opt/sum_server")
    env = system_svc._updater_env()

    # Both fields are required by Settings, so their absence is a hard failure.
    assert env["SUM_SERVER_DATABASE_URL"] == get_settings().database_url
    assert env["SUM_SERVER_SIGNING_PRIVATE_KEY"] == get_settings().signing_private_key
    assert env["SUM_SERVER_INSTALL_DIR"] == "/opt/sum_server"
    # The updater must not start its own GitHub polling loop.
    assert env["SUM_SERVER_UPDATE_CHECK_ENABLED"] == "false"


def test_env_file_is_root_only_and_outside_the_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stray file in install_dir would dirty the tree and abort the update."""
    install = tmp_path / "checkout"
    data = tmp_path / "data"
    install.mkdir()
    _reload_settings(
        monkeypatch,
        SUM_SERVER_INSTALL_DIR=str(install),
        SUM_SERVER_DATA_DIR=str(data),
    )

    path = system_svc.write_updater_env_file()

    assert path.parent == data
    assert install not in path.parents
    assert path.stat().st_mode & 0o777 == 0o600
    body = path.read_text(encoding="utf-8")
    assert "SUM_SERVER_DATABASE_URL=" in body


def test_env_file_quotes_values(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A DSN carries characters systemd would otherwise interpret."""
    _reload_settings(
        monkeypatch,
        SUM_SERVER_DATA_DIR=str(tmp_path),
        SUM_SERVER_EXTERNAL_URL='https://example.com/a"b',
    )
    body = system_svc.write_updater_env_file().read_text(encoding="utf-8")
    assert 'SUM_SERVER_EXTERNAL_URL="https://example.com/a\\"b"' in body


# --- uv resolution ----------------------------------------------------------


def test_resolve_uv_prefers_the_explicit_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\n")
    _reload_settings(monkeypatch, SUM_SERVER_UV_BIN=str(uv))
    assert system_svc.resolve_uv() == str(uv)


def test_resolve_uv_reports_missing_configured_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _reload_settings(monkeypatch, SUM_SERVER_UV_BIN=str(tmp_path / "nope"))
    assert system_svc.resolve_uv() is None


def test_missing_uv_is_refused_before_the_update_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Better a refusal on the Settings page than a rollback mid-update."""
    _reload_settings(
        monkeypatch,
        SUM_SERVER_INSTALL_DIR=str(tmp_path),
        SUM_SERVER_UV_BIN=str(tmp_path / "missing-uv"),
    )
    monkeypatch.setattr(system_svc.shutil, "which", lambda _name: "/usr/bin/systemd-run")
    monkeypatch.setattr(system_svc.os, "geteuid", lambda: 0)

    ok, reason = system_svc.self_update_available()
    assert ok is False
    assert "uv not found" in reason


def test_shell_runner_uses_the_resolved_uv(tmp_path: Path) -> None:
    from sum_server.updater.shell import ShellRunner

    runner = ShellRunner(
        install_dir=tmp_path,
        service_name="sum-server",
        database_url="postgresql+asyncpg://x/y",
        health_url="https://127.0.0.1",
        uv_bin="/root/.local/bin/uv",
    )
    assert runner._uv == "/root/.local/bin/uv"
    # alembic was already absolute; keep it that way.
    assert runner._alembic == str(tmp_path / ".venv" / "bin" / "alembic")


# --- abandoned rows ---------------------------------------------------------


def _row(status: str, *, created_ago: int, started_ago: int | None) -> ServerUpdate:
    now = dt.datetime.now(tz=dt.UTC)
    return ServerUpdate(
        from_version="0.4.0",
        to_version="0.5.0",
        status=status,
        created_at=now - dt.timedelta(seconds=created_ago),
        started_at=None if started_ago is None else now - dt.timedelta(seconds=started_ago),
    )


def test_fresh_queued_row_is_not_abandoned() -> None:
    assert system_svc.is_abandoned(_row("queued", created_ago=5, started_ago=None)) is False


def test_queued_row_whose_updater_never_checked_in_is_abandoned() -> None:
    """The exact shape left behind when the updater dies building its settings."""
    row = _row("queued", created_ago=system_svc.ABANDONED_QUEUED_SECONDS + 1, started_ago=None)
    assert system_svc.is_abandoned(row) is True


def test_running_row_is_kept_while_it_is_plausibly_working() -> None:
    row = _row("migrating", created_ago=600, started_ago=300)
    assert system_svc.is_abandoned(row) is False


def test_long_silent_running_row_is_abandoned() -> None:
    row = _row(
        "migrating",
        created_ago=system_svc.ABANDONED_RUNNING_SECONDS + 100,
        started_ago=system_svc.ABANDONED_RUNNING_SECONDS + 1,
    )
    assert system_svc.is_abandoned(row) is True


def test_terminal_rows_are_never_abandoned() -> None:
    for status in ("success", "rolled_back", "failed"):
        row = _row(status, created_ago=10**6, started_ago=10**6)
        assert system_svc.is_abandoned(row) is False, status
