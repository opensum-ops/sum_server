"""Effective-parameter resolution: pure functions, no I/O.

Precedence (lowest to highest), modeled on ansible/Foreman inheritance:

1. The ``global`` root group.
2. Ancestor groups, nearer the root first: a child overrides its parent.
3. At equal depth, groups apply in name order, so the alphabetically last
   group wins a tie. Deterministic, if inelegant; avoid same-key conflicts
   between sibling groups.
4. The host's own parameters override everything.

Each resolved value carries provenance (which group or host supplied it).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GroupNode:
    """Minimal group shape the resolver needs."""

    id: uuid.UUID
    name: str
    parent_id: uuid.UUID | None


@dataclass(frozen=True)
class EffectiveParameter:
    key: str
    value: Any
    source_kind: str  # "group" | "host"
    source_name: str | None  # group name, or None for host


def ancestor_chain(
    group_id: uuid.UUID, groups_by_id: Mapping[uuid.UUID, GroupNode]
) -> list[GroupNode]:
    """Root-first chain from the root down to ``group_id`` (inclusive)."""
    chain: list[GroupNode] = []
    seen: set[uuid.UUID] = set()
    current: uuid.UUID | None = group_id
    while current is not None:
        if current in seen:  # pragma: no cover - cycles are rejected on write
            raise ValueError("group tree contains a cycle")
        seen.add(current)
        node = groups_by_id[current]
        chain.append(node)
        current = node.parent_id
    chain.reverse()
    return chain


def depth_of(group_id: uuid.UUID, groups_by_id: Mapping[uuid.UUID, GroupNode]) -> int:
    return len(ancestor_chain(group_id, groups_by_id)) - 1


def resolve_parameters(
    *,
    groups_by_id: Mapping[uuid.UUID, GroupNode],
    group_params: Mapping[uuid.UUID, Mapping[str, Any]],
    host_params: Mapping[str, Any],
    member_group_ids: Sequence[uuid.UUID],
    global_group_id: uuid.UUID,
) -> dict[str, EffectiveParameter]:
    """Merge parameters for one host. Later application wins."""
    contributing: set[uuid.UUID] = {global_group_id}
    for gid in member_group_ids:
        contributing.update(n.id for n in ancestor_chain(gid, groups_by_id))

    ordered = sorted(
        (groups_by_id[gid] for gid in contributing),
        key=lambda n: (depth_of(n.id, groups_by_id), n.name),
    )

    effective: dict[str, EffectiveParameter] = {}
    for node in ordered:
        for key, value in group_params.get(node.id, {}).items():
            effective[key] = EffectiveParameter(
                key=key, value=value, source_kind="group", source_name=node.name
            )
    for key, value in host_params.items():
        effective[key] = EffectiveParameter(
            key=key, value=value, source_kind="host", source_name=None
        )
    return effective
