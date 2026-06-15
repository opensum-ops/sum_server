"""Server model + ownership association tables."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Column, ForeignKey, Index, String, Table
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sum_server.core.db import Base, IdMixin, TimestampMixin

if TYPE_CHECKING:
    from sum_server.auth.models import AgentToken
    from sum_server.components.models import Component

SERVER_STATUS_VALUES = ("provisioning", "active", "decommissioned")

server_owner_users = Table(
    "server_owner_users",
    Base.metadata,
    Column("server_id", ForeignKey("servers.id", ondelete="CASCADE"), primary_key=True),
    Column("user_id", ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Index("ix_server_owner_users_user", "user_id"),
)

server_owner_teams = Table(
    "server_owner_teams",
    Base.metadata,
    Column("server_id", ForeignKey("servers.id", ondelete="CASCADE"), primary_key=True),
    Column("team_id", ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True),
    Index("ix_server_owner_teams_team", "team_id"),
)


class Server(Base, IdMixin, TimestampMixin):
    __tablename__ = "servers"

    name: Mapped[str] = mapped_column(String(256), nullable=False)
    hostname: Mapped[str | None] = mapped_column(String(256), nullable=True)
    description: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="provisioning")
    version: Mapped[int] = mapped_column(nullable=False, default=1)

    agent_tokens: Mapped[list[AgentToken]] = relationship(
        back_populates="server", cascade="all, delete-orphan", lazy="select"
    )
    components: Mapped[list[Component]] = relationship(
        back_populates="server", cascade="all, delete-orphan", lazy="select"
    )

    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (Index("ix_servers_status", "status"),)
