"""Presence derivation: live state of a host, computed at read time.

Presence is never stored; it is derived from the host's lifecycle status,
its last heartbeat, and the goodbye state (``reported_presence``) the agent
sent before going down. No sweeper needed: staleness is evaluated on read.

States:

- ``pending`` — never heartbeated (freshly created / not yet enrolled+running).
- ``online`` — heartbeat within the online window.
- ``unreachable`` — heartbeat stale with no goodbye (crash or network issue),
  or a reported reboot that outlived the reboot grace period.
- ``rebooting`` — agent reported a reboot and the grace period has not passed.
- ``powered_off`` — agent reported a clean power-off.
- ``stopped`` — agent stopped cleanly but the host stayed up.
- ``decommissioned`` — lifecycle wins over any signal.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sum_server.hosts.models import Host

PRESENCE_VALUES = (
    "pending",
    "online",
    "unreachable",
    "rebooting",
    "powered_off",
    "stopped",
    "decommissioned",
)


def derive_presence(
    host: Host,
    *,
    now: dt.datetime | None = None,
    online_window_seconds: int | None = None,
    reboot_grace_seconds: int | None = None,
) -> str:
    """Compute the presence state for ``host``. Pure function of row + clock."""
    from sum_server.settings import get_settings

    if host.status == "decommissioned":
        return "decommissioned"
    if host.last_heartbeat_at is None:
        return "pending"

    settings = get_settings()
    if online_window_seconds is None:
        online_window_seconds = settings.presence_online_window_seconds
    if reboot_grace_seconds is None:
        reboot_grace_seconds = settings.presence_reboot_grace_seconds
    if now is None:
        now = dt.datetime.now(tz=dt.UTC)
    age = (now - host.last_heartbeat_at).total_seconds()

    if host.reported_presence == "rebooting":
        return "rebooting" if age <= reboot_grace_seconds else "unreachable"
    if host.reported_presence in ("powered_off", "stopped"):
        return host.reported_presence
    return "online" if age <= online_window_seconds else "unreachable"
