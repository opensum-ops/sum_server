"""Team + TeamMembership models."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sum_server.core.db import Base, IdMixin, TimestampMixin

if TYPE_CHECKING:
    from sum_server.users.models import User


class Team(Base, IdMixin, TimestampMixin):
    __tablename__ = "teams"

    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    version: Mapped[int] = mapped_column(nullable=False, default=1)

    memberships: Mapped[list[TeamMembership]] = relationship(
        back_populates="team", cascade="all, delete-orphan", lazy="select"
    )

    __mapper_args__ = {"version_id_col": version}


class TeamMembership(Base, IdMixin, TimestampMixin):
    __tablename__ = "team_memberships"

    team_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")

    team: Mapped[Team] = relationship(back_populates="memberships", lazy="joined")
    user: Mapped[User] = relationship(back_populates="memberships", lazy="joined")

    __table_args__ = (
        UniqueConstraint("team_id", "user_id", name="uq_team_memberships_team_user"),
        Index("ix_team_memberships_user", "user_id"),
    )
