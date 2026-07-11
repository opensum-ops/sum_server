from __future__ import annotations

import pytest

from sum_server.jobs.capabilities import (
    known_capabilities,
    validate_payload,
)


def test_known_capabilities_returns_mvp_set() -> None:
    caps = known_capabilities()
    assert "rename_nic" in caps
    assert "mount_disk" in caps


def test_validate_rename_nic_ok() -> None:
    out = validate_payload("rename_nic", {"current_name": "eth0", "new_name": "ens1"})
    assert out == {"current_name": "eth0", "new_name": "ens1"}


def test_unknown_capability_raises() -> None:
    with pytest.raises(ValueError):
        validate_payload("does_not_exist", {})


def test_validate_mount_disk_defaults_options() -> None:
    out = validate_payload(
        "mount_disk",
        {"device": "/dev/sda1", "mountpoint": "/data", "fstype": "ext4"},
    )
    assert out["options"] == "defaults"


def test_validate_rejects_extra_fields() -> None:
    with pytest.raises(Exception):  # pydantic ValidationError or our wrapper
        validate_payload(
            "rename_nic",
            {"current_name": "eth0", "new_name": "ens1", "extra": "nope"},
        )
