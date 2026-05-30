"""SessionToken (user sessions) + AgentToken (long-lived agent identity)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sum_server.core.db import Base, IdMixin, TimestampMixin

if TYPE_CHECKING:
    from sum_server.servers.models import Server
    from sum_server.users.models import User


class SessionToken(Base, IdMixin, TimestampMixin):
    __tablename__ = "session_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions", lazy="joined")

    __table_args__ = (Index("ix_session_tokens_user", "user_id"),)


class AgentToken(Base, IdMixin, TimestampMixin):
    __tablename__ = "agent_tokens"

    server_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)

    server: Mapped[Server] = relationship(back_populates="agent_tokens", lazy="joined")

    __table_args__ = (Index("ix_agent_tokens_server", "server_id"),)
