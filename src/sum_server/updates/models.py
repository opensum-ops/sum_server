"""Update-related persistence: cached GitHub releases, runtime settings,
and the durable server-update status row.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sum_server.core.db import Base, IdMixin, TimestampMixin

# Component keys used in release_cache.repo and update flows.
COMPONENT_SERVER = "sum_server"
COMPONENT_AGENT = "sum_agent"

# Server self-update lifecycle. Terminal states: success, rolled_back, failed.
SERVER_UPDATE_STATUS_VALUES = (
    "queued",
    "snapshotting",
    "checking_out",
    "syncing",
    "migrating",
    "restarting",
    "verifying",
    "success",
    "rolling_back",
    "rolled_back",
    "failed",
)
_TERMINAL_STATUSES = frozenset({"success", "rolled_back", "failed"})


def is_terminal(status: str) -> bool:
    return status in _TERMINAL_STATUSES


class ReleaseCache(Base, TimestampMixin):
    """Latest known GitHub release per component. One row per ``repo``."""

    __tablename__ = "release_cache"

    repo: Mapped[str] = mapped_column(String(32), primary_key=True)
    latest_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(16384), nullable=True)
    published_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    assets: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    checked_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)


class SystemSetting(Base, TimestampMixin):
    """DB-backed runtime toggles (env stays the source for immutable config)."""

    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)


class ServerUpdate(Base, IdMixin, TimestampMixin):
    """One server self-update attempt. The out-of-process updater advances this
    row (durable across the sum-server restart); the UI polls it.
    """

    __tablename__ = "server_updates"

    from_version: Mapped[str] = mapped_column(String(32), nullable=False)
    to_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    detail: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    dump_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    started_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
