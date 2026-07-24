"""Update API schemas."""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict


class ComponentUpdateStatus(BaseModel):
    component: str
    current_version: str
    latest_version: str | None
    update_available: bool
    release_name: str | None = None
    notes: str | None = None
    published_at: dt.datetime | None = None
    checked_at: dt.datetime | None = None
    error: str | None = None


class UpdatesSummary(BaseModel):
    server: ComponentUpdateStatus
    agent: ComponentUpdateStatus


class ServerUpdateStatus(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    from_version: str
    to_version: str
    status: str
    detail: str | None
    started_at: dt.datetime | None
    finished_at: dt.datetime | None
    created_at: dt.datetime
