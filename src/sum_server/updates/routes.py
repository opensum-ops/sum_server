"""Update API routes: release summary + manual check + server self-update."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from sum_server.core.db import SessionDep
from sum_server.core.deps import AdminActor, UserActor
from sum_server.core.errors import NotFoundError
from sum_server.updates import service as svc
from sum_server.updates import system as system_svc
from sum_server.updates.schemas import ServerUpdateStatus, UpdatesSummary

router = APIRouter(prefix="/updates", tags=["updates"])


@router.get("", response_model=UpdatesSummary)
async def get_updates(_actor: UserActor, session: SessionDep) -> UpdatesSummary:
    return await svc.build_summary(session)


@router.post("/check", response_model=UpdatesSummary)
async def check_updates(_admin: AdminActor, session: SessionDep) -> UpdatesSummary:
    async with session.begin():
        await svc.refresh_all(session)
    return await svc.build_summary(session)


# --- Server self-update -----------------------------------------------------

system_router = APIRouter(prefix="/system", tags=["system"])


class ServerUpdateRequest(BaseModel):
    target_version: str


@system_router.post("/update", response_model=ServerUpdateStatus)
async def start_server_update(
    payload: ServerUpdateRequest, admin: AdminActor, session: SessionDep
) -> ServerUpdateStatus:
    async with session.begin():
        row = await system_svc.request_server_update(
            session, target_version=payload.target_version, actor=admin
        )
        status = ServerUpdateStatus.model_validate(row)
    await system_svc.launch_updater()
    return status


@system_router.get("/update/status", response_model=ServerUpdateStatus)
async def server_update_status(_admin: AdminActor, session: SessionDep) -> ServerUpdateStatus:
    row = await system_svc.latest_update(session)
    if row is None:
        raise NotFoundError("no server update on record")
    return ServerUpdateStatus.model_validate(row)
