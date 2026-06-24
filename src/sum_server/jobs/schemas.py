"""Job schemas (admin-facing + agent-facing)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

JobStatus = Literal["pending", "picked_up", "completed", "failed", "expired"]


class JobCreate(BaseModel):
    capability: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any]
    ttl_seconds: int | None = Field(default=None, ge=10, le=86400)


class JobResponse(BaseModel):
    """Job as returned over the wire. ``nonce`` and ``signature`` are base64-encoded."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    server_id: uuid.UUID
    capability: str
    payload: dict[str, Any]
    expires_at: dt.datetime
    status: JobStatus
    picked_up_at: dt.datetime | None
    created_at: dt.datetime
    nonce: str
    signature: str


class JobResultReport(BaseModel):
    status: Literal["completed", "failed"]
    exit_code: int | None = None
    output: dict[str, Any] = Field(default_factory=dict)


class JobResultResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    job_id: uuid.UUID
    status: JobStatus
    exit_code: int | None
    output: dict[str, Any]
    reported_at: dt.datetime
