"""Host request/response schemas."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

HostStatus = Literal["provisioning", "active", "decommissioned"]


class HostCreate(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    hostname: str | None = Field(default=None, max_length=256)
    description: str | None = Field(default=None, max_length=2048)
    status: HostStatus = "provisioning"


class HostUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    hostname: str | None = Field(default=None, max_length=256)
    description: str | None = Field(default=None, max_length=2048)
    status: HostStatus | None = None


class HostResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    hostname: str | None
    description: str | None
    status: HostStatus
    version: int
    created_at: dt.datetime


class HostWithOwnersResponse(HostResponse):
    user_owners: list[uuid.UUID] = []
    team_owners: list[uuid.UUID] = []


class OwnerAddRequest(BaseModel):
    user_id: uuid.UUID | None = None
    team_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> OwnerAddRequest:
        if (self.user_id is None) == (self.team_id is None):
            raise ValueError("exactly one of user_id or team_id must be set")
        return self
