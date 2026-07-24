"""Update API schemas."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel


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
