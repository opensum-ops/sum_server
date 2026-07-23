"""Host model + ownership association tables."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Column, ForeignKey, Index, String, Table
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sum_server.core.db import Base, IdMixin, TimestampMixin

if TYPE_CHECKING:
    from sum_server.auth.models import AgentToken
    from sum_server.components.models import Component

HOST_STATUS_VALUES = ("provisioning", "active", "decommissioned")

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

    name: Mapped[str] = mapped_column(String(256), nullable=False)
    hostname: Mapped[str | None] = mapped_column(String(256), nullable=True)
    description: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="provisioning")
    version: Mapped[int] = mapped_column(nullable=False, default=1)

    agent_tokens: Mapped[list[AgentToken]] = relationship(
        back_populates="host", cascade="all, delete-orphan", lazy="select"
    )
    components: Mapped[list[Component]] = relationship(
        back_populates="host", cascade="all, delete-orphan", lazy="select"
    )

    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (Index("ix_hosts_status", "status"),)
