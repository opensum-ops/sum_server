"""Job services: create+sign, pickup (atomic), result reporting, expiry sweep."""

from __future__ import annotations

import base64
import datetime as dt
import os
import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sum_server.core.audit import write_audit
from sum_server.core.context import Actor
from sum_server.core.errors import ConflictError, ForbiddenError, NotFoundError
from sum_server.core.ids import new_id
from sum_server.core.security import signing
from sum_server.jobs.capabilities import validate_payload
from sum_server.jobs.models import Job, JobResult
from sum_server.settings import get_settings


def _utcnow() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def _signing_payload(job: Job) -> dict[str, Any]:
    """Canonical payload structure signed by ``sum_server`` and verified by agents."""
    return {
        "id": str(job.id),
        "server_id": str(job.server_id),
        "capability": job.capability,
        "payload": job.payload,
        "nonce": base64.b64encode(job.nonce).decode(),
        "expires_at": job.expires_at.replace(microsecond=0).isoformat(),
    }


async def get_job(session: AsyncSession, job_id: uuid.UUID) -> Job | None:
    return (await session.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()


async def list_jobs_for_server(
    session: AsyncSession,
    *,
    server_id: uuid.UUID,
    status_filter: str | None = None,
    limit: int = 100,
) -> list[Job]:
    stmt = select(Job).where(Job.server_id == server_id)
    if status_filter is not None:
        stmt = stmt.where(Job.status == status_filter)
    stmt = stmt.order_by(Job.created_at.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def create_job(
    session: AsyncSession,
    *,
    server_id: uuid.UUID,
    capability: str,
    payload: dict[str, Any],
    ttl_seconds: int | None,
    actor: Actor,
) -> Job:
    from sum_server.servers.service import get_server

    server = await get_server(session, server_id)
    if server is None:
        raise NotFoundError("server not found")
    if server.status == "decommissioned":
        raise ConflictError("cannot create jobs for a decommissioned server")
    try:
        normalized = validate_payload(capability, payload)
    except ValueError as exc:
        raise ConflictError(str(exc)) from exc

    now = _utcnow()
    settings = get_settings()
    ttl = ttl_seconds or settings.job_default_ttl_seconds
    job = Job(
        id=new_id(),
        server_id=server_id,
        capability=capability,
        payload=normalized,
        nonce=os.urandom(16),
        expires_at=now + dt.timedelta(seconds=ttl),
        status="pending",
        signature=b"",
        created_by_actor_kind=actor.kind,
        created_by_actor_id=actor.id,
    )
    job.signature = signing.sign(_signing_payload(job))
    session.add(job)
    await write_audit(
        session,
        action="job.create",
        target_kind="job",
        target_id=job.id,
        payload={
            "server_id": str(server_id),
            "capability": capability,
            "expires_at": job.expires_at.isoformat(),
        },
    )
    return job


async def pickup_job(
    session: AsyncSession, *, job_id: uuid.UUID, agent_server_id: uuid.UUID
) -> Job:
    """Atomically claim a pending job for an agent.

    Idempotent: re-picking up an already-picked-up job by the same agent
    returns the job unchanged. Misaddressed pickup is audited and rejected.
    """
    job = await get_job(session, job_id)
    if job is None:
        raise NotFoundError("job not found")
    if job.server_id != agent_server_id:
        await write_audit(
            session,
            action="job.pickup_misaddressed",
            target_kind="job",
            target_id=job_id,
            payload={"agent_server_id": str(agent_server_id)},
        )
        raise ForbiddenError("job is not addressed to this agent")

    now = _utcnow()
    if job.status == "pending" and job.expires_at <= now:
        job.status = "expired"
        raise ConflictError("job expired before pickup")
    if job.status == "picked_up":
        return job
    if job.status != "pending":
        raise ConflictError(f"job is in terminal state: {job.status}")

    result = await session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == "pending")
        .values(status="picked_up", picked_up_at=now)
    )
    if (result.rowcount or 0) == 0:
        raise ConflictError("job was picked up by another worker")
    await session.refresh(job)
    await write_audit(
        session,
        action="job.picked_up",
        target_kind="job",
        target_id=job_id,
        payload={},
    )
    return job


async def report_result(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    agent_server_id: uuid.UUID,
    status: str,
    exit_code: int | None,
    output: dict[str, Any],
) -> JobResult:
    job = await get_job(session, job_id)
    if job is None:
        raise NotFoundError("job not found")
    if job.server_id != agent_server_id:
        await write_audit(
            session,
            action="job.result_misaddressed",
            target_kind="job",
            target_id=job_id,
            payload={"agent_server_id": str(agent_server_id)},
        )
        raise ForbiddenError("job is not addressed to this agent")
    if status not in ("completed", "failed"):
        raise ConflictError("status must be 'completed' or 'failed'")

    now = _utcnow()
    existing = (
        await session.execute(select(JobResult).where(JobResult.job_id == job_id))
    ).scalar_one_or_none()
    if existing is not None:
        identical = (
            existing.status == status
            and existing.exit_code == exit_code
            and existing.output == output
        )
        if not identical:
            raise ConflictError("a different result was already reported")
        return existing

    job_was_expired = job.expires_at <= now or job.status == "expired"
    if job_was_expired:
        await write_audit(
            session,
            action="job.result_after_expiry",
            target_kind="job",
            target_id=job_id,
            payload={"reported_status": status},
        )
    else:
        job.status = status

    res = JobResult(
        job_id=job_id,
        status=status,
        exit_code=exit_code,
        output=output,
        reported_at=now,
    )
    session.add(res)
    await write_audit(
        session,
        action=f"job.{status}",
        target_kind="job",
        target_id=job_id,
        payload={"exit_code": exit_code, "expired": job_was_expired},
    )
    return res


async def sweep_expired(session: AsyncSession) -> int:
    """Mark any pending jobs past ``expires_at`` as ``expired``. Returns the count."""
    now = _utcnow()
    result = await session.execute(
        update(Job).where(Job.status == "pending", Job.expires_at <= now).values(status="expired")
    )
    return int(result.rowcount or 0)


async def pending_jobs_for_server(
    session: AsyncSession, *, server_id: uuid.UUID, limit: int = 50
) -> list[Job]:
    """Non-expired, pending jobs for ``server_id``, oldest first."""
    now = _utcnow()
    stmt = (
        select(Job)
        .where(
            Job.server_id == server_id,
            Job.status == "pending",
            Job.expires_at > now,
        )
        .order_by(Job.created_at)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


def to_response_payload(job: Job) -> dict[str, Any]:
    """Build a JSON-serializable dict (with base64 nonce/signature) for ``JobResponse``."""
    return {
        "id": job.id,
        "server_id": job.server_id,
        "capability": job.capability,
        "payload": job.payload,
        "expires_at": job.expires_at,
        "status": job.status,
        "picked_up_at": job.picked_up_at,
        "created_at": job.created_at,
        "nonce": base64.b64encode(job.nonce).decode(),
        "signature": base64.b64encode(job.signature).decode(),
    }
