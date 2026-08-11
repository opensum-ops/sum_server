"""Field-level diffing of a component against an observed snapshot.

Pure, so it is unit-testable: the point is that a disk growing reads as
``attrs.size_bytes`` rather than as an unreadable dict-versus-dict.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sum_server.components.models import Component
from sum_server.components.schemas import ComponentIngest
from sum_server.components.service import component_label, diff_component

# What a stored row looks like: whatever the previous ingest's model_dump wrote,
# so it carries every field of the schema including defaults.
STORED_ATTRS: dict[str, Any] = {
    "kind": "disk",
    "size_bytes": 100,
    "rotation_rpm": 0,
    "bus": "nvme",
    "wwn": None,
}


def _stored(**kw: Any) -> Component:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "host_id": uuid.uuid4(),
        "kind": "disk",
        "vendor": "acme",
        "model": "D1",
        "serial": "S1",
        "slot": "nvme0n1",
        "present": True,
        "attrs": dict(STORED_ATTRS),
        "first_seen": dt.datetime.now(tz=dt.UTC),
        "last_seen": dt.datetime.now(tz=dt.UTC),
    }
    base.update(kw)
    return Component(**base)


def _observed(**kw: Any) -> ComponentIngest:
    attrs: dict[str, Any] = {"kind": "disk", "size_bytes": 100, "bus": "nvme"}
    attrs.update(kw.pop("attrs", {}))
    base: dict[str, Any] = {
        "kind": "disk",
        "vendor": "acme",
        "model": "D1",
        "serial": "S1",
        "slot": "nvme0n1",
        "attrs": attrs,
    }
    base.update(kw)
    return ComponentIngest(**base)


def test_no_change_is_no_history() -> None:
    """The common case: a snapshot identical to the last one writes nothing."""
    assert diff_component(_stored(), _observed()) == []


def test_top_level_field_change() -> None:
    assert diff_component(_stored(), _observed(model="D2")) == [("model", "D1", "D2")]


def test_attrs_are_compared_per_key() -> None:
    changes = diff_component(_stored(), _observed(attrs={"size_bytes": 200}))
    assert changes == [("attrs.size_bytes", 100, 200)]


def test_several_fields_at_once() -> None:
    changes = diff_component(_stored(), _observed(vendor="other", attrs={"bus": "sata"}))
    assert ("vendor", "acme", "other") in changes
    assert ("attrs.bus", "nvme", "sata") in changes


def test_attr_appearing_reads_as_a_change_from_nothing() -> None:
    changes = diff_component(_stored(), _observed(attrs={"wwn": "0x5000"}))
    assert changes == [("attrs.wwn", None, "0x5000")]


def test_attr_the_agent_stopped_reporting() -> None:
    """Stored data can carry a key the current schema no longer emits."""
    stale = dict(STORED_ATTRS)
    stale["legacy_key"] = "gone"
    changes = diff_component(_stored(attrs=stale), _observed())
    assert changes == [("attrs.legacy_key", "gone", None)]


def test_reappearing_hardware_is_a_change() -> None:
    assert ("present", False, True) in diff_component(_stored(present=False), _observed())


def test_serial_is_not_diffed() -> None:
    """A different serial is a different component, handled by the swap path."""
    changes = diff_component(_stored(), _observed(serial="S2"))
    assert all(field != "serial" for field, _, _ in changes)


def test_label_prefers_the_name_a_human_would_use() -> None:
    assert component_label("disk", "nvme0n1", "S1", "D1") == "nvme0n1"
    assert component_label("disk", None, "S1", "D1") == "S1"
    assert component_label("disk", None, None, "D1") == "D1"
    assert component_label("gpu", None, None, None) == "gpu"
