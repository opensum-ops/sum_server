"""Update-related persistence: cached GitHub releases + runtime settings.

(The durable server-update status row lands with the self-update feature.)
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sum_server.core.db import Base, TimestampMixin

# Component keys used in release_cache.repo and update flows.
COMPONENT_SERVER = "sum_server"
COMPONENT_AGENT = "sum_agent"


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
