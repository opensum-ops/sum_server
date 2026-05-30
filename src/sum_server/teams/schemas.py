"""Team request/response schemas."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TeamRole = Literal["member", "admin"]


class TeamCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1024)


class TeamUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1024)


class TeamResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    description: str | None
    created_at: dt.datetime


class TeamMemberAdd(BaseModel):
    user_id: uuid.UUID
    role: TeamRole = "member"


class TeamMemberUpdate(BaseModel):
    role: TeamRole


class TeamMembershipResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    team_id: uuid.UUID
    user_id: uuid.UUID
    role: TeamRole
    created_at: dt.datetime
