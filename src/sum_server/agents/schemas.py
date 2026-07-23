"""Agent schemas (enrollment, inventory ingest)."""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, Field

from sum_server.components.schemas import ComponentIngest


class EnrollmentCreate(BaseModel):
    host_id: uuid.UUID
    ttl_seconds: int | None = Field(default=None, ge=60, le=86400)


class EnrollmentCreateResponse(BaseModel):
    id: uuid.UUID
    enrollment_token: str
    expires_at: dt.datetime


class EnrollmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    host_id: uuid.UUID
    expires_at: dt.datetime
    used_at: dt.datetime | None
    revoked_at: dt.datetime | None


class EnrollRequest(BaseModel):
    enrollment_token: str


class EnrollResponse(BaseModel):
    agent_token: str
    host_id: uuid.UUID
    signing_public_key: str


class InventoryIngestRequest(BaseModel):
    components: list[ComponentIngest]


class InventoryIngestResponse(BaseModel):
    created: int
    updated: int
    marked_absent: int
    swaps: int
