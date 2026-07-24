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
        else:
            if row.value != value:
                row.value = value
                updated.append(key)
            row.last_seen = now

    removed = sorted(set(existing) - set(facts))
    for key in removed:
        await session.delete(existing[key])

    # Adopt the observed hostname onto the host row (hostname-first display).
    hostname = facts.get("hostname")
    if isinstance(hostname, str) and hostname and hostname != host.hostname:
        host.hostname = hostname

    return {"created": sorted(created), "updated": sorted(updated), "removed": removed}
