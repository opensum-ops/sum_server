"""Agent routes: enrollment lifecycle + agent-side (inventory)."""

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
)
from sum_server.components.service import ingest_inventory
from sum_server.core.db import SessionDep
from sum_server.core.deps import AdminActor, AgentActor
from sum_server.core.security.signing import get_public_key_b64

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


# Agent-side: inventory ----------------------------------------------------------


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
