"""Host search: visibility-scoped filtering across facts, groups, components,
parameters, and presence.

Fact, group, and component filters compile to SQL (EXISTS subqueries).
Presence and effective-parameter filters resolve in Python over the
SQL-narrowed set: presence is derived and parameters are inherited, so
neither exists as a queryable column. Fine at the current fleet scale;
revisit if host counts make the Python pass noticeable.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import ColumnElement, Select, false, literal, or_, select, true
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from sum_server.components.models import Component
from sum_server.groups.models import Group, host_groups
from sum_server.hosts.models import Host, HostFact

MAX_RESULTS = 500

# Always fetched for the host list's own columns, on top of any filtered keys.
DISPLAY_FACT_KEYS = ("primary_ipv4",)


@dataclass(frozen=True)
class HostSearch:
    """Parsed filter set. Empty fields are inactive."""

    text: str | None = None
    presence: str | None = None
    group: str | None = None
    component: str | None = None
    facts: tuple[tuple[str, str], ...] = ()
    parameters: tuple[tuple[str, str], ...] = ()


@dataclass
class HostSearchResult:
    host: Host
    # Values for the fact/parameter keys being filtered on (extra columns).
    fact_values: dict[str, Any] = field(default_factory=dict)
    param_values: dict[str, Any] = field(default_factory=dict)
    matched_components: list[Component] = field(default_factory=list)
    # Values for DISPLAY_FACT_KEYS, shown as fixed columns.
    display_facts: dict[str, Any] = field(default_factory=dict)


def _coerced_json_values(raw: str) -> list[Any]:
    """Candidate typed values a text filter should match (string, number, bool)."""
    candidates: list[Any] = [raw]
    lowered = raw.lower()
    if lowered in ("true", "false"):
        candidates.append(lowered == "true")
    try:
        candidates.append(int(raw))
    except ValueError:
        with contextlib.suppress(ValueError):
            candidates.append(float(raw))
    return candidates


def _apply_sql_filters(stmt: Select[tuple[Host]], search: HostSearch) -> Select[tuple[Host]]:
    if search.text:
        like = f"%{search.text}%"
        stmt = stmt.where(
            or_(
                Host.name.ilike(like),
                Host.hostname.ilike(like),
                Host.description.ilike(like),
            )
        )
    for key, value in search.facts:
        # Typed JSONB literals: a bare string binds as VARCHAR, which JSONB
        # equality rejects.
        candidates = [literal(v, type_=JSONB) for v in _coerced_json_values(value)]
        fact_match = select(HostFact.id).where(
            HostFact.host_id == Host.id,
            HostFact.key == key,
            or_(*[HostFact.value == c for c in candidates]),
        )
        stmt = stmt.where(fact_match.exists())
    if search.group:
        member = (
            select(host_groups.c.host_id)
            .join(Group, Group.id == host_groups.c.group_id)
            .where(host_groups.c.host_id == Host.id, Group.name == search.group)
        )
        stmt = stmt.where(member.exists())
    if search.component:
        like = f"%{search.component}%"
        comp = select(Component.id).where(
            Component.host_id == Host.id,
            Component.present.is_(True),
            or_(
                Component.kind.ilike(like),
                Component.vendor.ilike(like),
                Component.model.ilike(like),
                Component.serial.ilike(like),
            ),
        )
        stmt = stmt.where(comp.exists())
    return stmt


async def visibility_clause(
    session: AsyncSession, *, actor_user_id: uuid.UUID
) -> ColumnElement[bool]:
    """WHERE clause restricting a ``Host`` query to what this actor may read.

    Admins get an always-true clause; an **unknown user gets always-false**, so a
    caller that forgets to special-case a missing user denies rather than leaks.
    Reused by the suggestion endpoints so they cannot enumerate values from
    hosts the actor cannot see.
    """
    from sum_server.hosts.models import host_owner_teams, host_owner_users
    from sum_server.teams.models import TeamMembership
    from sum_server.users.service import get_user

    user = await get_user(session, actor_user_id)
    if user is None:
        return false()
    if user.is_admin:
        return true()
    return or_(
        Host.id.in_(
            select(host_owner_users.c.host_id).where(host_owner_users.c.user_id == actor_user_id)
        ),
        Host.id.in_(
            select(host_owner_teams.c.host_id)
            .join(TeamMembership, TeamMembership.team_id == host_owner_teams.c.team_id)
            .where(TeamMembership.user_id == actor_user_id)
        ),
    )


async def search_hosts(
    session: AsyncSession,
    *,
    actor_user_id: uuid.UUID,
    search: HostSearch,
    limit: int = MAX_RESULTS,
) -> list[HostSearchResult]:
    from sum_server.groups.service import effective_parameters_for_host

    stmt = select(Host).where(await visibility_clause(session, actor_user_id=actor_user_id))
    stmt = _apply_sql_filters(stmt, search)
    stmt = stmt.order_by(Host.hostname.nulls_last(), Host.name).limit(limit)
    hosts = list((await session.execute(stmt)).scalars().all())

    results: list[HostSearchResult] = []
    fact_keys = [k for k, _ in search.facts]
    param_filters = list(search.parameters)
    for host in hosts:
        if search.presence and host.presence != search.presence:
            continue
        result = HostSearchResult(host=host)
        if param_filters:
            effective = {
                p.key: p.value
                for p in await effective_parameters_for_host(session, host_id=host.id)
            }
            if any(str(effective.get(k)) != v for k, v in param_filters):
                continue
            result.param_values = {k: effective.get(k) for k, _ in param_filters}
        results.append(result)

    # Extra columns: fact values and matched components for the survivors. The
    # filtered keys and the always-shown display keys come back in one query.
    if results:
        ids = [r.host.id for r in results]
        wanted = set(fact_keys) | set(DISPLAY_FACT_KEYS)
        rows = (
            await session.execute(
                select(HostFact).where(HostFact.host_id.in_(ids), HostFact.key.in_(wanted))
            )
        ).scalars()
        by_host: dict[uuid.UUID, dict[str, Any]] = {}
        for f in rows:
            by_host.setdefault(f.host_id, {})[f.key] = f.value
        for r in results:
            values = by_host.get(r.host.id, {})
            r.fact_values = {k: values[k] for k in fact_keys if k in values}
            r.display_facts = {k: values[k] for k in DISPLAY_FACT_KEYS if k in values}
    if search.component and results:
        like = f"%{search.component}%"
        ids = [r.host.id for r in results]
        comp_rows = (
            await session.execute(
                select(Component).where(
                    Component.host_id.in_(ids),
                    Component.present.is_(True),
                    or_(
                        Component.kind.ilike(like),
                        Component.vendor.ilike(like),
                        Component.model.ilike(like),
                        Component.serial.ilike(like),
                    ),
                )
            )
        ).scalars()
        comp_by_host: dict[uuid.UUID, list[Component]] = {}
        for c in comp_rows:
            comp_by_host.setdefault(c.host_id, []).append(c)
        for r in results:
            r.matched_components = comp_by_host.get(r.host.id, [])
    return results
