"""Update API routes: read the summary, trigger a manual check."""

from __future__ import annotations

from fastapi import APIRouter

from sum_server.core.db import SessionDep
from sum_server.core.deps import AdminActor, UserActor
from sum_server.updates import service as svc
from sum_server.updates.schemas import UpdatesSummary

router = APIRouter(prefix="/updates", tags=["updates"])


@router.get("", response_model=UpdatesSummary)
async def get_updates(_actor: UserActor, session: SessionDep) -> UpdatesSummary:
    return await svc.build_summary(session)


@router.post("/check", response_model=UpdatesSummary)
async def check_updates(_admin: AdminActor, session: SessionDep) -> UpdatesSummary:
    async with session.begin():
        await svc.refresh_all(session)
    return await svc.build_summary(session)
