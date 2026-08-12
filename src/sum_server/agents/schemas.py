"""Agent schemas (enrollment, inventory ingest, heartbeat)."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sum_server.components.schemas import ComponentIngest

# Fact keys: lowercase snake, dots allowed for namespacing (e.g. "os.version").
_FACT_KEY_RE = re.compile(r"[a-z][a-z0-9_.]{0,63}")

# JSON scalar or list of strings; nested objects are deliberately not allowed.
FactValue = str | int | float | bool | None | list[str]


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
    facts: dict[str, FactValue] = Field(default_factory=dict)
    components: list[ComponentIngest]
    # Additive (N-1 agents omit it); the agent version also arrives via
    # User-Agent, so this is a robustness belt, not a requirement.
    agent_version: str | None = Field(default=None, max_length=32)

    @field_validator("facts")
    @classmethod
    def _validate_facts(cls, v: dict[str, FactValue]) -> dict[str, FactValue]:
        for key, value in v.items():
            if not _FACT_KEY_RE.fullmatch(key):
                raise ValueError(f"invalid fact key: {key!r}")
            if isinstance(value, str) and len(value) > 1024:
                raise ValueError(f"fact {key!r}: string value too long (max 1024)")
            if isinstance(value, list):
                if len(value) > 64:
                    raise ValueError(f"fact {key!r}: list too long (max 64 items)")
                if any(len(item) > 256 for item in value):
                    raise ValueError(f"fact {key!r}: list item too long (max 256)")
        return v


class InventoryIngestResponse(BaseModel):
    created: int
    updated: int
    marked_absent: int
    swaps: int
    facts_created: int
    facts_updated: int
    facts_removed: int


class HeartbeatRequest(BaseModel):
    state: Literal["running", "stopping"] = "running"
    # Why the agent is stopping; ignored (and meaningless) while running.
    # `agent_removed` is the goodbye an agent sends as its last act before
    # uninstalling itself, and is the server's only completion signal: once
    # gone, it can never report anything again.
    detail: Literal["rebooting", "powered_off", "agent_stop", "agent_removed"] | None = None
    boot_id: str | None = Field(default=None, max_length=64)
    # Additive (N-1 agents omit it). Lets the server confirm a completed
    # self-update without waiting for the next inventory.
    agent_version: str | None = Field(default=None, max_length=32)


class AgentUpdateDirective(BaseModel):
    target_version: str
    sha256: str
    binary_url: str
    signature: str  # base64 Ed25519 over {host_id, target_version, sha256}


class AgentRemoveDirective(BaseModel):
    action: str
    requested_at: str
    signature: str  # base64 Ed25519 over {host_id, action, requested_at}


class HeartbeatResponse(BaseModel):
    presence: str
    server_time: dt.datetime
    # Additive: present only when the host has a pending agent update. N-1
    # agents ignore the field entirely.
    agent_update: AgentUpdateDirective | None = None
    # Additive, same contract. An agent too old to know this field keeps
    # running, so removal only works from the version that ships it onward;
    # `uninstall.sh` is the answer for anything older.
    agent_remove: AgentRemoveDirective | None = None
