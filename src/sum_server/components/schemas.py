"""Component schemas (polymorphic ``attrs`` via discriminated union)."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

ComponentKind = Literal["disk", "nic", "cpu", "gpu", "memory"]


class _BaseAttrs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiskAttrs(_BaseAttrs):
    kind: Literal["disk"]
    size_bytes: int = Field(ge=0)
    rotation_rpm: int = Field(ge=0, default=0)
    bus: Literal["sata", "nvme", "sas", "usb", "scsi", "unknown"] = "unknown"
    wwn: str | None = None


class NicAttrs(_BaseAttrs):
    kind: Literal["nic"]
    mac: str
    speed_mbps: int = Field(ge=0)
    driver: str | None = None
    pci_addr: str | None = None

    @field_validator("mac")
    @classmethod
    def _canon_mac(cls, v: str) -> str:
        v = v.lower().replace("-", ":").strip()
        if not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", v):
            raise ValueError("mac must be 6 lowercase colon-separated hex bytes")
        return v


class CpuAttrs(_BaseAttrs):
    kind: Literal["cpu"]
    cores: int = Field(ge=1)
    threads: int = Field(ge=1)
    base_hz: int = Field(ge=0)
    microarch: str | None = None


class GpuAttrs(_BaseAttrs):
    kind: Literal["gpu"]
    vram_bytes: int = Field(ge=0)
    driver_version: str | None = None
    pci_addr: str | None = None


class MemoryAttrs(_BaseAttrs):
    kind: Literal["memory"]
    size_bytes: int = Field(ge=0)
    speed_mts: int = Field(ge=0)
    form_factor: str | None = None
    slot: str | None = None


ComponentAttrs = Annotated[
    DiskAttrs | NicAttrs | CpuAttrs | GpuAttrs | MemoryAttrs, Field(discriminator="kind")
]


class ComponentIngest(BaseModel):
    """A single inventory entry submitted by an agent."""

    model_config = ConfigDict(extra="forbid")

    kind: ComponentKind
    vendor: str | None = Field(default=None, max_length=128)
    model: str | None = Field(default=None, max_length=256)
    serial: str | None = Field(default=None, max_length=256)
    slot: str | None = Field(default=None, max_length=64)
    attrs: ComponentAttrs

    @field_validator("attrs")
    @classmethod
    def _attrs_matches_kind(cls, v: ComponentAttrs, info: ValidationInfo) -> ComponentAttrs:
        outer = info.data.get("kind")
        if outer is not None and v.kind != outer:
            raise ValueError(f"attrs.kind ({v.kind}) does not match outer kind ({outer})")
        return v


class ComponentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    server_id: uuid.UUID
    kind: ComponentKind
    vendor: str | None
    model: str | None
    serial: str | None
    slot: str | None
    present: bool
    attrs: dict[str, Any]
    first_seen: dt.datetime
    last_seen: dt.datetime
