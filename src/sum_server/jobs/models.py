"""Job + JobResult models."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, LargeBinary, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sum_server.core.db import Base, IdMixin, TimestampMixin

JOB_STATUS_VALUES = ("pending", "picked_up", "completed", "failed", "expired")


class Job(Base, IdMixin, TimestampMixin):
    __tablename__ = "jobs"

    server_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("servers.id", ondelete="RESTRICT"), nullable=False
    )
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary(16), nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    signature: Mapped[bytes] = mapped_column(LargeBinary(64), nullable=False)
    picked_up_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    created_by_actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_actor_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    result: Mapped[JobResult | None] = relationship(
        back_populates="job", cascade="all, delete-orphan", uselist=False, lazy="joined"
    )

    __table_args__ = (
        Index("ix_jobs_server_status", "server_id", "status"),
        Index("ix_jobs_expires", "expires_at"),
    )


class JobResult(Base):
    __tablename__ = "job_results"

    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    exit_code: Mapped[int | None] = mapped_column(nullable=True)
    output: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    reported_at: Mapped[dt.datetime] = mapped_column(
        default=lambda: dt.datetime.now(tz=dt.UTC), nullable=False
    )

    job: Mapped[Job] = relationship(back_populates="result")
