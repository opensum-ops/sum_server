"""AgentEnrollment: one-time tokens that exchange for long-lived AgentTokens."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from sum_server.core.db import Base, IdMixin, TimestampMixin


class AgentEnrollment(Base, IdMixin, TimestampMixin):
    __tablename__ = "agent_enrollments"

    server_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    used_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    created_by_actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_actor_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
