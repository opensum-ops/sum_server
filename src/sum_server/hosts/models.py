"""Host model, facts, and ownership association tables."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import Column, ForeignKey, Index, String, Table, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sum_server.core.db import Base, IdMixin, TimestampMixin

if TYPE_CHECKING:
    from sum_server.auth.models import AgentToken
    from sum_server.components.models import Component

HOST_STATUS_VALUES = ("provisioning", "active", "decommissioned")

# Goodbye states an agent can report before going down. ``None`` means running.
REPORTED_PRESENCE_VALUES = ("rebooting", "powered_off", "stopped")

host_owner_users = Table(
    "host_owner_users",
    Base.metadata,
    Column("host_id", ForeignKey("hosts.id", ondelete="CASCADE"), primary_key=True),
    Column("user_id", ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Index("ix_host_owner_users_user", "user_id"),
)

host_owner_teams = Table(
    "host_owner_teams",
    Base.metadata,
    Column("host_id", ForeignKey("hosts.id", ondelete="CASCADE"), primary_key=True),
    Column("team_id", ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True),
    Index("ix_host_owner_teams_team", "team_id"),
)


class Host(Base, IdMixin, TimestampMixin):
    __tablename__ = "hosts"

    # The single identity field. Seeded with the operator's label at enrollment
    # and overwritten by the agent's observed hostname on first inventory, so a
    # host is only ever known by one name.
    hostname: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="provisioning")
    version: Mapped[int] = mapped_column(nullable=False, default=1)

    # Presence signal (see hosts/presence.py for the derivation).
    last_heartbeat_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    reported_presence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    boot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Desired agent version (set by "Update agent"); cleared once the agent
    # reports it reached it. See updates/directive.py.
    target_agent_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    agent_tokens: Mapped[list[AgentToken]] = relationship(
        back_populates="host", cascade="all, delete-orphan", lazy="select"
    )
    components: Mapped[list[Component]] = relationship(
        back_populates="host", cascade="all, delete-orphan", lazy="select"
    )
    facts: Mapped[list[HostFact]] = relationship(
        back_populates="host", cascade="all, delete-orphan", lazy="select"
    )

    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (Index("ix_hosts_status", "status"),)

    @property
    def presence(self) -> str:
        """Derived live state (see hosts/presence.py). Not stored."""
        from sum_server.hosts.presence import derive_presence

        return derive_presence(self)


class HostFact(Base, IdMixin):
    """A single agent-observed fact (``key`` -> JSON ``value``) about a host."""

    __tablename__ = "host_facts"

    host_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("hosts.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    first_seen: Mapped[dt.datetime] = mapped_column(
        default=lambda: dt.datetime.now(tz=dt.UTC), nullable=False
    )
    last_seen: Mapped[dt.datetime] = mapped_column(
        default=lambda: dt.datetime.now(tz=dt.UTC), nullable=False
    )

    host: Mapped[Host] = relationship(back_populates="facts", lazy="joined")

    __table_args__ = (
        UniqueConstraint("host_id", "key"),
        Index("ix_host_facts_key", "key"),
    )
