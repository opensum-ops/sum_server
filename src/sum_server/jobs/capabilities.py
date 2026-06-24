"""Capability registry.

Each capability has a string name and a Pydantic schema for its payload. The
registry is consulted both server-side when admins create a job and client-side
by the agent's SDK before executing one.

Adding a capability: define a payload schema (subclass ``_Cap``) and register it
in ``_REGISTRY``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Cap(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RenameNicPayload(_Cap):
    current_name: str = Field(min_length=1, max_length=64)
    new_name: str = Field(min_length=1, max_length=64)


class MountDiskPayload(_Cap):
    device: str = Field(min_length=1, max_length=256)
    mountpoint: str = Field(min_length=1, max_length=1024)
    fstype: str = Field(min_length=1, max_length=32)
    options: str = Field(default="defaults", max_length=512)


_REGISTRY: dict[str, type[_Cap]] = {
    "rename_nic": RenameNicPayload,
    "mount_disk": MountDiskPayload,
}


def known_capabilities() -> list[str]:
    return sorted(_REGISTRY)


def get_schema(capability: str) -> type[_Cap] | None:
    return _REGISTRY.get(capability)


def validate_payload(capability: str, raw: dict[str, Any]) -> dict[str, Any]:
    schema = _REGISTRY.get(capability)
    if schema is None:
        raise ValueError(f"unknown capability: {capability}")
    return schema(**raw).model_dump()
