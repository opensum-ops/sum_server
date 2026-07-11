"""Agent enrollment services + helpers."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sum_server.agents.models import AgentEnrollment
from sum_server.auth.service import mint_agent_token
from sum_server.core.audit import write_audit
from sum_server.core.context import Actor
from sum_server.core.errors import EnrollmentError, NotFoundError
from sum_server.core.ids import new_id
from sum_server.core.security.tokens import hash_token, mint_token
from sum_server.settings import get_settings


def _utcnow() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


async def create_enrollment(
    session: AsyncSession,
    *,
    server_id: uuid.UUID,
    actor: Actor,
    ttl_seconds: int | None = None,
) -> tuple[str, AgentEnrollment]:
    """Create a one-time enrollment token. Returns ``(raw_token, row)``."""
    from sum_server.servers.service import get_server

    server = await get_server(session, server_id)
    if server is None:
        raise NotFoundError("server not found")
    settings = get_settings()
    ttl = ttl_seconds or settings.enrollment_token_ttl_seconds
    raw, token_hash = mint_token()
    enr = AgentEnrollment(
        id=new_id(),
        server_id=server_id,
        token_hash=token_hash,
        expires_at=_utcnow() + dt.timedelta(seconds=ttl),
        created_by_actor_kind=actor.kind,
        created_by_actor_id=actor.id,
    )
    session.add(enr)
    await write_audit(
        session,
        action="agent.enrollment_created",
        target_kind="server",
        target_id=server_id,
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
            target_kind="server",
            target_id=enr.server_id,
            payload={"enrollment_id": str(enr.id)},
        )
    return enr


async def consume_enrollment(
    session: AsyncSession, *, raw_token: str, ip: str | None
) -> tuple[str, uuid.UUID]:
    """Exchange a one-time enrollment token for a long-lived agent token.

    Atomic against concurrent consumption. Returns ``(raw_agent_token, server_id)``.
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

    raw_agent_token, _ = await mint_agent_token(session, server_id=enr.server_id, ip=ip)
    await write_audit(
        session,
        action="agent.enrolled",
        target_kind="server",
        target_id=enr.server_id,
        payload={"enrollment_id": str(enr.id), "ip": ip},
        actor_kind="agent",
        actor_id=enr.server_id,
    )
    return raw_agent_token, enr.server_id


async def list_enrollments_for_server(
    session: AsyncSession, *, server_id: uuid.UUID
) -> list[AgentEnrollment]:
    return list(
        (
            await session.execute(
                select(AgentEnrollment)
                .where(AgentEnrollment.server_id == server_id)
                .order_by(AgentEnrollment.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
