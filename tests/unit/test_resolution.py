"""Parameter-resolution precedence matrix (pure, no DB).

Tree used throughout::

    global
    ├── dc-east
    │   └── web          (host is a member)
    └── linux            (host is a member)
"""

from __future__ import annotations

import uuid

import pytest

from sum_server.groups.resolution import GroupNode, ancestor_chain, resolve_parameters

GLOBAL = uuid.uuid4()
DC_EAST = uuid.uuid4()
WEB = uuid.uuid4()
LINUX = uuid.uuid4()

NODES = {
    GLOBAL: GroupNode(id=GLOBAL, name="global", parent_id=None),
    DC_EAST: GroupNode(id=DC_EAST, name="dc-east", parent_id=GLOBAL),
    WEB: GroupNode(id=WEB, name="web", parent_id=DC_EAST),
    LINUX: GroupNode(id=LINUX, name="linux", parent_id=GLOBAL),
}


def _resolve(
    group_params: dict[uuid.UUID, dict[str, object]],
    host_params: dict[str, object] | None = None,
    members: list[uuid.UUID] | None = None,
) -> dict[str, tuple[object, str, str | None]]:
    resolved = resolve_parameters(
        groups_by_id=NODES,
        group_params=group_params,
        host_params=host_params or {},
        member_group_ids=members if members is not None else [WEB, LINUX],
        global_group_id=GLOBAL,
    )
    return {k: (p.value, p.source_kind, p.source_name) for k, p in resolved.items()}


def test_ancestor_chain_is_root_first() -> None:
    assert [n.name for n in ancestor_chain(WEB, NODES)] == ["global", "dc-east", "web"]


def test_ancestor_chain_cycle_detected() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    nodes = {
        a: GroupNode(id=a, name="a", parent_id=b),
        b: GroupNode(id=b, name="b", parent_id=a),
    }
    with pytest.raises(ValueError, match="cycle"):
        ancestor_chain(a, nodes)


def test_global_applies_to_everyone() -> None:
    out = _resolve({GLOBAL: {"ntp": "pool.ntp.org"}}, members=[])
    assert out["ntp"] == ("pool.ntp.org", "group", "global")


def test_child_overrides_parent() -> None:
    out = _resolve(
        {
            GLOBAL: {"ntp": "pool.ntp.org"},
            DC_EAST: {"ntp": "ntp.east.internal"},
            WEB: {"ntp": "ntp.web.internal"},
        }
    )
    assert out["ntp"] == ("ntp.web.internal", "group", "web")


def test_intermediate_ancestor_contributes() -> None:
    # dc-east is not a direct membership, but web's chain pulls it in.
    out = _resolve({DC_EAST: {"syslog": "syslog.east"}}, members=[WEB])
    assert out["syslog"] == ("syslog.east", "group", "dc-east")


def test_equal_depth_tie_goes_to_alphabetically_last() -> None:
    # dc-east and linux are both depth 1; "linux" sorts after "dc-east".
    out = _resolve(
        {DC_EAST: {"motd": "east"}, LINUX: {"motd": "linux"}},
        members=[DC_EAST, LINUX],
    )
    assert out["motd"] == ("linux", "group", "linux")


def test_deeper_wins_over_equal_depth_name_order() -> None:
    # web (depth 2) beats linux (depth 1) even though "web" sorts after.
    out = _resolve({WEB: {"motd": "web"}, LINUX: {"motd": "linux"}})
    assert out["motd"] == ("web", "group", "web")


def test_host_params_override_everything() -> None:
    out = _resolve(
        {GLOBAL: {"ntp": "pool.ntp.org"}, WEB: {"ntp": "ntp.web.internal"}},
        host_params={"ntp": "10.0.0.1"},
    )
    assert out["ntp"] == ("10.0.0.1", "host", None)


def test_disjoint_keys_all_present() -> None:
    out = _resolve(
        {GLOBAL: {"a": 1}, DC_EAST: {"b": 2}, WEB: {"c": 3}, LINUX: {"d": 4}},
        host_params={"e": 5},
    )
    assert {k: v[0] for k, v in out.items()} == {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5}


def test_non_member_groups_do_not_contribute() -> None:
    out = _resolve({WEB: {"role": "web"}}, members=[LINUX])
    assert "role" not in out
