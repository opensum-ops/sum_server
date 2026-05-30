"""User request/response schemas."""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserCreate(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=8, max_length=1024)
    is_admin: bool = False


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    email: EmailStr | None = None
    password: str | None = Field(default=None, min_length=8, max_length=1024)
    is_admin: bool | None = None  # admin-only field; service enforces


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    display_name: str
    is_admin: bool
    created_at: dt.datetime
    deleted_at: dt.datetime | None = None
