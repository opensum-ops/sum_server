"""Component model (polymorphic inventory via single table + ``kind`` discriminator)."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sum_server.core.db import Base, IdMixin, TimestampMixin

if TYPE_CHECKING:
    from sum_server.servers.models import Server

COMPONENT_KINDS = ("disk", "nic", "cpu", "gpu", "memory")

class Component(Base, IdMixin, TimestampMixin):
    __tablename__ = "components"

    server_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    vendor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model: Mapped[str | None] = mapped_column(String(256), nullable=True)
    serial: Mapped[str | None] = mapped_column(String(256), nullable=True)
    slot: Mapped[str | None] = mapped_column(String(64), nullable=True)
    present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    attrs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    first_seen: Mapped[dt.datetime] = mapped_column(
        default=lambda: dt.datetime.now(tz=dt.UTC), nullable=False
    )
    last_seen: Mapped[dt.datetime] = mapped_column(
        default=lambda: dt.datetime.now(tz=dt.UTC), nullable=False
    )

    server: Mapped[Server] = relationship(back_populates="components", lazy="joined")

    __table_args__ = (
        Index(
            "uq_components_server_kind_serial",
            "server_id",
            "kind",
            "serial",
            unique=True,
            postgresql_where=text("serial IS NOT NULL"),
        ),
        Index(
            "uq_components_server_kind_slot",
            "server_id",
            "kind",
            "slot",
            unique=True,
            postgresql_where=text("serial IS NULL AND slot IS NOT NULL"),
        ),
        Index("ix_components_server_kind", "server_id", "kind"),
    )
