"""Agent enrollment services + helpers."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sum_server.agents.models import AgentEnrollment

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sum_server.components.schemas import ComponentIngest
    from sum_server.hosts.models import Host
from sum_server.auth.service import mint_agent_token
from sum_server.core.audit import write_audit
from sum_server.core.context import Actor
from sum_server.core.errors import EnrollmentError, NotFoundError
from sum_server.core.ids import new_id
from sum_server.core.security.tokens import hash_token, mint_token
from sum_server.history import service as history
from sum_server.settings import get_settings


def _utcnow() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


async def create_enrollment(
    session: AsyncSession,
    *,
    host_id: uuid.UUID,
    actor: Actor,
    ttl_seconds: int | None = None,
) -> tuple[str, AgentEnrollment]:
    """Create a one-time enrollment token. Returns ``(raw_token, row)``."""
    from sum_server.hosts.service import get_host

    host = await get_host(session, host_id)
    if host is None:
        raise NotFoundError("host not found")
    settings = get_settings()
    ttl = ttl_seconds or settings.enrollment_token_ttl_seconds
    raw, token_hash = mint_token()
    enr = AgentEnrollment(
        id=new_id(),
        host_id=host_id,
        token_hash=token_hash,
        expires_at=_utcnow() + dt.timedelta(seconds=ttl),
        created_by_actor_kind=actor.kind,
        created_by_actor_id=actor.id,
    )
    session.add(enr)
    await write_audit(
        session,
        action="agent.enrollment_created",
        target_kind="host",
        target_id=host_id,
        payload={"enrollment_id": str(enr.id), "ttl_seconds": ttl},
    )
    return raw, enr


async def revoke_enrollment(session: AsyncSession, *, enrollment_id: uuid.UUID) -> AgentEnrollment:
    enr = (
        await session.execute(select(AgentEnrollment).where(AgentEnrollment.id == enrollment_id))
    ).scalar_one_or_none()
    if enr is None:
        raise NotFoundError("enrollment not found")
    if enr.revoked_at is None and enr.used_at is None:
        enr.revoked_at = _utcnow()
        await write_audit(
            session,
            action="agent.enrollment_revoked",
            target_kind="host",
            target_id=enr.host_id,
            payload={"enrollment_id": str(enr.id)},
        )
    return enr


async def consume_enrollment(
    session: AsyncSession, *, raw_token: str, ip: str | None
) -> tuple[str, uuid.UUID]:
    """Exchange a one-time enrollment token for a long-lived agent token.

    Atomic against concurrent consumption. Returns ``(raw_agent_token, host_id)``.
    """
    token_hash = hash_token(raw_token)
    enr = (
        await session.execute(
            select(AgentEnrollment).where(AgentEnrollment.token_hash == token_hash)
        )
    ).scalar_one_or_none()
    if enr is None:
        raise EnrollmentError("invalid enrollment token")
    now = _utcnow()
    if enr.revoked_at is not None:
        raise EnrollmentError("enrollment was revoked")
    if enr.expires_at <= now:
        raise EnrollmentError("enrollment expired")
    if enr.used_at is not None:
        raise EnrollmentError("enrollment already used")

    result = await session.execute(
        update(AgentEnrollment)
        .where(AgentEnrollment.id == enr.id, AgentEnrollment.used_at.is_(None))
        .values(used_at=now)
    )
    if (result.rowcount or 0) == 0:  # type: ignore[attr-defined]
        raise EnrollmentError("enrollment was concurrently consumed")

    raw_agent_token, _ = await mint_agent_token(session, host_id=enr.host_id, ip=ip)

    # A successfully enrolled host is no longer provisioning.
    from sum_server.hosts.service import get_host

    host = await get_host(session, enr.host_id)
    if host is not None and host.status == "provisioning":
        host.status = "active"

    await write_audit(
        session,
        action="agent.enrolled",
        target_kind="host",
        target_id=enr.host_id,
        payload={"enrollment_id": str(enr.id), "ip": ip},
        actor_kind="agent",
        actor_id=enr.host_id,
    )
    return raw_agent_token, enr.host_id


async def ingest_full_inventory(
    session: AsyncSession,
    *,
    host_id: uuid.UUID,
    facts: dict[str, object],
    components: Sequence[ComponentIngest],
) -> dict[str, int]:
    """Ingest a full agent snapshot (facts + components) and audit once."""
    from sum_server.components.service import ingest_inventory
    from sum_server.hosts.facts import ingest_facts
    from sum_server.hosts.service import get_host

    host = await get_host(session, host_id)
    if host is None:
        raise NotFoundError("host not found")

    fact_changes = await ingest_facts(session, host=host, facts=dict(facts))
    counts = await ingest_inventory(session, host_id=host.id, entries=components)
    counts["facts_created"] = len(fact_changes["created"])
    counts["facts_updated"] = len(fact_changes["updated"])
    counts["facts_removed"] = len(fact_changes["removed"])

    await write_audit(
        session,
        action="agent.inventory_submitted",
        target_kind="host",
        target_id=host.id,
        payload={**counts, "fact_changes": {k: v for k, v in fact_changes.items() if v}},
    )
    return counts


_GOODBYE_DETAIL_TO_PRESENCE = {
    "rebooting": "rebooting",
    "powered_off": "powered_off",
    "agent_stop": "stopped",
    None: "stopped",
}


async def record_heartbeat(
    session: AsyncSession,
    *,
    host_id: uuid.UUID,
    state: str,
    detail: str | None,
    boot_id: str | None,
) -> Host:
    """Record a heartbeat (running) or goodbye (stopping) from an agent.

    A running heartbeat clears any goodbye state. A ``boot_id`` change without
    a preceding reboot/power-off goodbye means the host went down uncleanly
    (crash or power loss) and is audited as ``host.unclean_reboot``.
    """
    from sum_server.hosts.service import get_host

    host = await get_host(session, host_id)
    if host is None:
        raise NotFoundError("host not found")
    now = _utcnow()

    if state == "running":
        if (
            boot_id
            and host.boot_id
            and boot_id != host.boot_id
            and host.reported_presence not in ("rebooting", "powered_off")
        ):
            await write_audit(
                session,
                action="host.unclean_reboot",
                target_kind="host",
                target_id=host.id,
                payload={"old_boot_id": host.boot_id, "new_boot_id": boot_id},
            )
        if boot_id and boot_id != host.boot_id:
            # A reboot is worth a history row; the heartbeat that carries it is
            # not. last_heartbeat_at moves every 30s and is deliberately never
            # recorded, or the table would be nothing but heartbeats.
            history.record(
                session,
                host_id=host.id,
                scope="host",
                field="boot_id",
                change="add" if host.boot_id is None else "edit",
                old=host.boot_id,
                new=boot_id,
                at=now,
            )
        if boot_id:
            host.boot_id = boot_id
        host.reported_presence = None
    else:  # stopping
        host.reported_presence = _GOODBYE_DETAIL_TO_PRESENCE.get(detail, "stopped")
        await write_audit(
            session,
            action="host.reported_shutdown",
            target_kind="host",
            target_id=host.id,
            payload={"detail": detail, "presence": host.reported_presence},
        )

    host.last_heartbeat_at = now
    return host


async def list_enrollments_for_host(
    session: AsyncSession, *, host_id: uuid.UUID
) -> list[AgentEnrollment]:
    return list(
        (
            await session.execute(
                select(AgentEnrollment)
                .where(AgentEnrollment.host_id == host_id)
                .order_by(AgentEnrollment.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
