"""Auth request/response schemas."""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    expires_at: dt.datetime


class MeResponse(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    is_admin: bool
    created_at: dt.datetime


class AgentTokenResponse(BaseModel):
    agent_token: str
    server_id: uuid.UUID
    signing_public_key: str
