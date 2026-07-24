"""Agent routes: enrollment lifecycle + agent-side (inventory, heartbeat)."""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Request, status
from fastapi.responses import FileResponse

from sum_server.agents import service as svc
from sum_server.agents.schemas import (
    EnrollmentCreate,
    EnrollmentCreateResponse,
    EnrollmentResponse,
    EnrollRequest,
    EnrollResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    InventoryIngestRequest,
    InventoryIngestResponse,
)
from sum_server.core.db import SessionDep
from sum_server.core.deps import AdminActor, AgentActor
from sum_server.core.errors import NotFoundError
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
            host_id=payload.host_id,
            actor=admin,
            ttl_seconds=payload.ttl_seconds,
        )
    return EnrollmentCreateResponse(id=enr.id, enrollment_token=raw, expires_at=enr.expires_at)


@router.get("/enrollments/for-host/{host_id}", response_model=list[EnrollmentResponse])
async def list_enrollments(
    host_id: uuid.UUID,
    _admin: AdminActor,
    session: SessionDep,
) -> list[EnrollmentResponse]:
    rows = await svc.list_enrollments_for_host(session, host_id=host_id)
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
        agent_token, host_id = await svc.consume_enrollment(
            session, raw_token=payload.enrollment_token, ip=ip
        )
    return EnrollResponse(
        agent_token=agent_token,
        host_id=host_id,
        signing_public_key=get_public_key_b64(),
    )


# Agent-side: inventory + heartbeat ----------------------------------------------


@router.post("/inventory", response_model=InventoryIngestResponse)
async def submit_inventory(
    payload: InventoryIngestRequest,
    agent: AgentActor,
    session: SessionDep,
) -> InventoryIngestResponse:
    assert agent.id is not None
    async with session.begin():
        counts = await svc.ingest_full_inventory(
            session,
            host_id=agent.id,
            facts=dict(payload.facts),
            components=payload.components,
        )
    return InventoryIngestResponse(**counts)


@router.post("/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(
    payload: HeartbeatRequest,
    agent: AgentActor,
    session: SessionDep,
    request: Request,
) -> HeartbeatResponse:
    from sum_server.agents.compat import agent_version_from_request
    from sum_server.hosts.facts import get_fact_value
    from sum_server.settings import get_settings
    from sum_server.updates.agent_update import build_directive_for_host

    assert agent.id is not None
    reported = agent_version_from_request(request, payload.agent_version)
    async with session.begin():
        host = await svc.record_heartbeat(
            session,
            host_id=agent.id,
            state=payload.state,
            detail=payload.detail,
            boot_id=payload.boot_id,
        )
        presence = host.presence
        directive = None
        if payload.state == "running" and host.target_agent_version:
            arch = await get_fact_value(session, host_id=agent.id, key="arch")
            base_url = get_settings().external_url or str(request.base_url)
            directive = await build_directive_for_host(
                session,
                host=host,
                reported_version=reported,
                host_arch=str(arch) if arch is not None else None,
                base_url=base_url,
            )
    return HeartbeatResponse(
        presence=presence,
        server_time=dt.datetime.now(tz=dt.UTC),
        agent_update=directive,
    )


@router.get("/binary/{version}")
async def get_agent_binary(
    version: str,
    _agent: AgentActor,
    session: SessionDep,
) -> FileResponse:
    """Stream a cached agent binary to an enrolled agent."""
    from sum_server.updates.agent_binary import cached_binary_if_present

    cached = cached_binary_if_present(version)
    if cached is None:
        raise NotFoundError("agent binary not available")
    return FileResponse(
        cached.path,
        media_type="application/octet-stream",
        filename=f"sum-agent-{version}",
    )
