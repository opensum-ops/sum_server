"""User model."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sum_server.core.db import Base, IdMixin, TimestampMixin

if TYPE_CHECKING:
    from sum_server.auth.models import SessionToken
    from sum_server.teams.models import TeamMembership


class User(Base, IdMixin, TimestampMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    version: Mapped[int] = mapped_column(nullable=False, default=1)

    sessions: Mapped[list[SessionToken]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="select"
    )
    memberships: Mapped[list[TeamMembership]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="select"
    )

    __mapper_args__ = {"version_id_col": version}
