"""Agent routes: enrollment lifecycle + agent-side (inventory, jobs)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request, status

from sum_server.agents import service as svc
from sum_server.agents.schemas import (
    EnrollmentCreate,
    EnrollmentCreateResponse,
    EnrollmentResponse,
    EnrollRequest,
    EnrollResponse,
    InventoryIngestRequest,
    InventoryIngestResponse,
    JobsListResponse,
)
from sum_server.components.service import ingest_inventory
from sum_server.core.db import SessionDep
from sum_server.core.deps import AdminActor, AgentActor
from sum_server.core.security.signing import get_public_key_b64
from sum_server.jobs import service as jobs_svc
from sum_server.jobs.schemas import JobResponse, JobResultReport, JobResultResponse

router = APIRouter(prefix="/agents", tags=["agents"])

# Admin: enrollment lifecycle ----------------------------------------


@router.post(
    "/enrollments",
    response_model=EnrollmentCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_enrollment(
    payload: EnrollmentCreate,
    admin: AdminActor,
    session: SessionDep,
) -> EnrollmentCreateResponse:
    async with session.begin():
        raw, enr = await svc.create_enrollment(
            session,
            server_id=payload.server_id,
            actor=admin,
            ttl_seconds=payload.ttl_seconds,
        )
    return EnrollmentCreateResponse(id=enr.id, enrollment_token=raw, expires_at=enr.expires_at)


@router.get("/enrollments/for-server/{server_id}", response_model=list[EnrollmentResponse])
async def list_enrollments(
    server_id: uuid.UUID,
    _admin: AdminActor,
    session: SessionDep,
) -> list[EnrollmentResponse]:
    rows = await svc.list_enrollments_for_server(session, server_id=server_id)
    return [EnrollmentResponse.model_validate(r) for r in rows]


@router.delete("/enrollments/{enrollment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_enrollment(
    enrollment_id: uuid.UUID,
    _admin: AdminActor,
    session: SessionDep,
) -> None:
    async with session.begin():
        await svc.revoke_enrollment(session, enrollment_id=enrollment_id)


# Public: enroll (exchange one-time token for agent token) ------------------------


@router.post("/enroll", response_model=EnrollResponse)
async def enroll(
    payload: EnrollRequest,
    request: Request,
    session: SessionDep,
) -> EnrollResponse:
    ip = request.client.host if request.client else None
    async with session.begin():
        agent_token, server_id = await svc.consume_enrollment(
            session, raw_token=payload.enrollment_token, ip=ip
        )
    return EnrollResponse(
        agent_token=agent_token,
        server_id=server_id,
        signing_public_key=get_public_key_b64(),
    )


# Agent-side: inventory + jobs ---------------------------------------------------


@router.post("/inventory", response_model=InventoryIngestResponse)
async def submit_inventory(
    payload: InventoryIngestRequest,
    agent: AgentActor,
    session: SessionDep,
) -> InventoryIngestResponse:
    assert agent.id is not None
    async with session.begin():
        counts = await ingest_inventory(session, server_id=agent.id, entries=payload.components)
    return InventoryIngestResponse(**counts)


@router.get("/jobs", response_model=JobsListResponse)
async def poll_jobs(
    agent: AgentActor,
    session: SessionDep,
    limit: int = 50,
) -> JobsListResponse:
    assert agent.id is not None
    rows = await jobs_svc.pending_jobs_for_server(session, server_id=agent.id, limit=limit)
    return JobsListResponse(
        jobs=[JobResponse.model_validate(jobs_svc.to_response_payload(j)) for j in rows]
    )


@router.post("/jobs/{job_id}/pickup", response_model=JobResponse)
async def pickup(
    job_id: uuid.UUID,
    agent: AgentActor,
    session: SessionDep,
) -> JobResponse:
    assert agent.id is not None
    async with session.begin():
        job = await jobs_svc.pickup_job(session, job_id=job_id, agent_server_id=agent.id)
    return JobResponse.model_validate(jobs_svc.to_response_payload(job))


@router.post("/jobs/{job_id}/result", response_model=JobResultResponse)
async def report_result(
    job_id: uuid.UUID,
    payload: JobResultReport,
    agent: AgentActor,
    session: SessionDep,
) -> JobResultResponse:
    assert agent.id is not None
    async with session.begin():
        result = await jobs_svc.report_result(
            session,
            job_id=job_id,
            agent_server_id=agent.id,
            status=payload.status,
            exit_code=payload.exit_code,
            output=payload.output,
        )
    return JobResultResponse.model_validate(result)
