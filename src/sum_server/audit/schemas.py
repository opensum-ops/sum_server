"""Audit read schemas."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict


class AuditEntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ts: dt.datetime
    actor_kind: str
    actor_id: uuid.UUID | None
    action: str
    target_kind: str
    target_id: uuid.UUID | None
    payload: dict[str, Any]
