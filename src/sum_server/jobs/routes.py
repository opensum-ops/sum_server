"""Job admin routes. Agent-facing endpoints live under ``agents/``."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from sum_server.core.db import SessionDep
from sum_server.core.deps import UserActor
from sum_server.core.errors import ForbiddenError, NotFoundError
from sum_server.jobs import service as svc
from sum_server.jobs.schemas import JobCreate, JobResponse
from sum_server.servers.service import get_server, user_can_read
from sum_server.users.service import get_user

router = APIRouter(tags=["jobs"])


async def _require_owner_or_admin(
    server_id: uuid.UUID, actor_id: uuid.UUID, session: SessionDep
) -> None:
    try:
        user = await get_user(session, actor_id)
        if user is not None and user.is_admin:
            return
        server = await get_server(session, server_id)
        if server is None:
            raise NotFoundError("server not found")
        if not await user_can_read(session, server, actor_id):
            raise ForbiddenError("not authorized for this server")
    finally:
        # Release the auto-begun read transaction so the handler owns its own
        # ``async with session.begin()``.
        await session.rollback()


@router.post(
    "/servers/{server_id}/jobs",
    response_model=JobResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_job(
    server_id: uuid.UUID,
    payload: JobCreate,
    actor: UserActor,
    session: SessionDep,
) -> JobResponse:
    assert actor.id is not None
    await _require_owner_or_admin(server_id, actor.id, session)
    async with session.begin():
        job = await svc.create_job(
            session,
            server_id=server_id,
            capability=payload.capability,
            payload=payload.payload,
            ttl_seconds=payload.ttl_seconds,
            actor=actor,
        )
    return JobResponse.model_validate(svc.to_response_payload(job))


@router.get("/servers/{server_id}/jobs", response_model=list[JobResponse])
async def list_jobs(
    server_id: uuid.UUID,
    actor: UserActor,
    session: SessionDep,
    status_filter: str | None = None,
) -> list[JobResponse]:
    assert actor.id is not None
    await _require_owner_or_admin(server_id, actor.id, session)
    rows = await svc.list_jobs_for_server(session, server_id=server_id, status_filter=status_filter)
    return [JobResponse.model_validate(svc.to_response_payload(j)) for j in rows]


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: uuid.UUID,
    actor: UserActor,
    session: SessionDep,
) -> JobResponse:
    job = await svc.get_job(session, job_id)
    if job is None:
        raise NotFoundError("job not found")
    assert actor.id is not None
    await _require_owner_or_admin(job.server_id, actor.id, session)
    return JobResponse.model_validate(svc.to_response_payload(job))
