"""Host fact services: snapshot ingest + reads.

Facts are agent-observed truth. Each ingest is a full snapshot: keys missing
from the snapshot are deleted (the agent stopped observing them). The special
``hostname`` fact is adopted onto the host row so the UI can display it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sum_server.core.ids import new_id
from sum_server.history import service as history
from sum_server.hosts.models import Host, HostFact


def _utcnow() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


async def list_facts(session: AsyncSession, *, host_id: uuid.UUID) -> list[HostFact]:
    return list(
        (
            await session.execute(
                select(HostFact).where(HostFact.host_id == host_id).order_by(HostFact.key)
            )
        )
        .scalars()
        .all()
    )


async def get_fact_value(session: AsyncSession, *, host_id: uuid.UUID, key: str) -> Any | None:
    """Return a single fact's value for a host, or ``None`` if unset."""
    return (
        await session.execute(
            select(HostFact.value).where(HostFact.host_id == host_id, HostFact.key == key)
        )
    ).scalar_one_or_none()


async def distinct_fact_keys(
    session: AsyncSession,
    *,
    actor_user_id: uuid.UUID,
    prefix: str = "",
    limit: int = 20,
) -> list[str]:
    """Fact keys observed on hosts this actor may read, for search suggestions.

    Scoped through :func:`~sum_server.hosts.search.visibility_clause`: without it
    the suggestions would let a non-admin enumerate keys from hosts they cannot
    see.
    """
    from sum_server.hosts.search import visibility_clause

    visible = select(Host.id).where(await visibility_clause(session, actor_user_id=actor_user_id))
    stmt = select(HostFact.key).where(HostFact.host_id.in_(visible))
    if prefix:
        stmt = stmt.where(HostFact.key.ilike(f"{prefix}%"))
    stmt = stmt.distinct().order_by(HostFact.key).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def distinct_fact_values(
    session: AsyncSession,
    *,
    actor_user_id: uuid.UUID,
    key: str,
    prefix: str = "",
    limit: int = 20,
) -> list[str]:
    """Values observed for one fact key, as display strings. Same scoping."""
    from sum_server.hosts.search import visibility_clause

    visible = select(Host.id).where(await visibility_clause(session, actor_user_id=actor_user_id))
    stmt = select(HostFact.value).where(HostFact.host_id.in_(visible), HostFact.key == key)
    rows = (await session.execute(stmt.distinct().limit(limit * 4))).scalars().all()

    # Values are JSONB, so stringify here rather than in SQL; a fact can hold a
    # number or bool, and the filter matches on the text form either way.
    seen: list[str] = []
    for value in rows:
        text = value if isinstance(value, str) else str(value)
        if prefix and not text.lower().startswith(prefix.lower()):
            continue
        if text not in seen:
            seen.append(text)
        if len(seen) >= limit:
            break
    return sorted(seen)


async def ingest_facts(
    session: AsyncSession,
    *,
    host: Host,
    facts: dict[str, Any],
) -> dict[str, list[str]]:
    """Upsert a fact snapshot for ``host``.

    Returns changed key names as ``{"created": [...], "updated": [...],
    "removed": [...]}`` for the caller's audit payload.
    """
    now = _utcnow()
    existing = {
        f.key: f
        for f in (
            await session.execute(select(HostFact).where(HostFact.host_id == host.id))
        ).scalars()
    }

    created: list[str] = []
    updated: list[str] = []
    for key, value in facts.items():
        row = existing.get(key)
        if row is None:
            session.add(
                HostFact(
                    id=new_id(),
                    host_id=host.id,
                    key=key,
                    value=value,
                    first_seen=now,
                    last_seen=now,
                )
            )
            created.append(key)
            history.record(
                session,
                host_id=host.id,
                scope="fact",
                field=key,
                change="add",
                new=value,
                at=now,
            )
        else:
            if row.value != value:
                # Read the old value before the overwrite; it is the whole
                # point of the history row.
                history.record(
                    session,
                    host_id=host.id,
                    scope="fact",
                    field=key,
                    change="edit",
                    old=row.value,
                    new=value,
                    at=now,
                )
                row.value = value
                updated.append(key)
            row.last_seen = now

    removed = sorted(set(existing) - set(facts))
    for key in removed:
        history.record(
            session,
            host_id=host.id,
            scope="fact",
            field=key,
            change="del",
            old=existing[key].value,
            at=now,
        )
        await session.delete(existing[key])

    # Adopt the observed hostname onto the host row (hostname-first display).
    hostname = facts.get("hostname")
    if isinstance(hostname, str) and hostname and hostname != host.hostname:
        history.record(
            session,
            host_id=host.id,
            scope="host",
            field="hostname",
            change="edit",
            old=host.hostname,
            new=hostname,
            at=now,
        )
        host.hostname = hostname

    return {"created": sorted(created), "updated": sorted(updated), "removed": removed}
