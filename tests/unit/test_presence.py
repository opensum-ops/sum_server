"""Presence derivation matrix (pure function, explicit clock and windows)."""

from __future__ import annotations

import datetime as dt

from sum_server.hosts.models import Host
from sum_server.hosts.presence import derive_presence

NOW = dt.datetime(2026, 7, 23, 12, 0, 0, tzinfo=dt.UTC)
WINDOW = 90
GRACE = 900


def _host(
    *,
    status: str = "active",
    heartbeat_age: float | None = None,
    reported: str | None = None,
) -> Host:
    h = Host(hostname="n1", status=status)
    if heartbeat_age is not None:
        h.last_heartbeat_at = NOW - dt.timedelta(seconds=heartbeat_age)
    h.reported_presence = reported
    return h


def _derive(h: Host) -> str:
    return derive_presence(h, now=NOW, online_window_seconds=WINDOW, reboot_grace_seconds=GRACE)


def test_decommissioned_wins() -> None:
    assert _derive(_host(status="decommissioned", heartbeat_age=1)) == "decommissioned"


def test_never_heartbeated_is_pending() -> None:
    assert _derive(_host()) == "pending"
    assert _derive(_host(status="provisioning")) == "pending"


def test_fresh_heartbeat_is_online() -> None:
    assert _derive(_host(heartbeat_age=1)) == "online"
    assert _derive(_host(heartbeat_age=WINDOW)) == "online"


def test_stale_heartbeat_is_unreachable() -> None:
    assert _derive(_host(heartbeat_age=WINDOW + 1)) == "unreachable"


def test_reported_reboot_within_grace() -> None:
    assert _derive(_host(heartbeat_age=GRACE, reported="rebooting")) == "rebooting"


def test_reported_reboot_past_grace_degrades() -> None:
    assert _derive(_host(heartbeat_age=GRACE + 1, reported="rebooting")) == "unreachable"


def test_reported_power_off_persists() -> None:
    assert _derive(_host(heartbeat_age=10 * GRACE, reported="powered_off")) == "powered_off"


def test_reported_stop_persists() -> None:
    assert _derive(_host(heartbeat_age=10 * GRACE, reported="stopped")) == "stopped"


def test_running_heartbeat_overrides_stale_goodbye() -> None:
    # reported_presence is cleared by a running heartbeat; None means running.
    assert _derive(_host(heartbeat_age=5, reported=None)) == "online"
