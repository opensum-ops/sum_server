"""Group + parameter request/response schemas."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Group names: ansible-style, lowercase slug.
_GROUP_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
# Parameter keys share the fact-key shape (dots allowed for namespacing).
_PARAM_KEY_RE = re.compile(r"[a-z][a-z0-9_.]{0,63}")


def validate_group_name(name: str) -> str:
    if not _GROUP_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid group name: {name!r}")
    return name


def validate_param_key(key: str) -> str:
    if not _PARAM_KEY_RE.fullmatch(key):
        raise ValueError(f"invalid parameter key: {key!r}")
    return key


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=1024)
    # Defaults to the global root when omitted.
    parent_id: uuid.UUID | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return validate_group_name(v)


class GroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=1024)
    parent_id: uuid.UUID | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        return None if v is None else validate_group_name(v)


class GroupResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    parent_id: uuid.UUID | None
    created_at: dt.datetime


class MemberAddRequest(BaseModel):
    host_id: uuid.UUID


class ParameterSet(BaseModel):
    value: Any


class ParameterResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    value: Any
    updated_at: dt.datetime


class EffectiveParameterResponse(BaseModel):
    key: str
    value: Any
    source_kind: str
    source_name: str | None
