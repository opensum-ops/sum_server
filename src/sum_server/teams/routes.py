"""Team routes."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, status
from sqlalchemy.ext.asyncio import AsyncSession

from sum_server.core.db import SessionDep
from sum_server.core.deps import AdminActor, UserActor
from sum_server.core.errors import ForbiddenError, NotFoundError
from sum_server.core.pagination import Cursor, Page, page_params
from sum_server.teams import service as svc
from sum_server.teams.schemas import (
    TeamCreate,
    TeamMemberAdd,
    TeamMembershipResponse,
    TeamMemberUpdate,
    TeamResponse,
    TeamUpdate,
)
from sum_server.users.service import get_user

router = APIRouter(prefix="/teams", tags=["teams"])


async def _is_org_admin(actor_id: uuid.UUID, session: AsyncSession) -> bool:
    user = await get_user(session, actor_id)
    return user is not None and user.is_admin


async def _require_team_admin_or_org_admin(
    actor_id: uuid.UUID, team_id: uuid.UUID, session: AsyncSession
) -> None:
    try:
        if await _is_org_admin(actor_id, session):
            return
        if await svc.is_team_admin(session, team_id=team_id, user_id=actor_id):
            return
    finally:
        # Release the auto-begun read transaction so the handler owns its own
        # ``async with session.begin()``.
        await session.rollback()
    raise ForbiddenError("team admins or org admins only")


def _parse_if_match(if_match: str | None) -> int | None:
    if if_match is None:
        return None
    try:
        return int(if_match.strip().strip('"'))
    except ValueError as exc:
        raise ForbiddenError("invalid If-Match") from exc


@router.post("", response_model=TeamResponse, status_code=status.HTTP_201_CREATED)
async def create_team(
    payload: TeamCreate,
    _admin: AdminActor,
    session: SessionDep,
) -> TeamResponse:
    async with session.begin():
        team = await svc.create_team(session, payload)
    return TeamResponse.model_validate(team)


@router.get("", response_model=Page[TeamResponse])
async def list_teams(
    _actor: UserActor,
    session: SessionDep,
    pagination: Annotated[tuple[int, object], Depends(page_params)],
) -> Page[TeamResponse]:
    limit, cursor = pagination
    rows = await svc.list_teams(session, limit=limit, cursor=cursor)  # type: ignore[arg-type]
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = (
        Cursor(id=items[-1].id, ts=items[-1].created_at).encode() if (has_more and items) else None
    )
    return Page[TeamResponse](
        items=[TeamResponse.model_validate(t) for t in items], next_cursor=next_cursor
    )


@router.get("/{team_id}", response_model=TeamResponse)
async def get_team(
    team_id: uuid.UUID,
    _actor: UserActor,
    session: SessionDep,
) -> TeamResponse:
    team = await svc.get_team(session, team_id)
    if team is None:
        raise NotFoundError("team not found")
    return TeamResponse.model_validate(team)


@router.patch("/{team_id}", response_model=TeamResponse)
async def update_team(
    team_id: uuid.UUID,
    payload: TeamUpdate,
    actor: UserActor,
    session: SessionDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> TeamResponse:
    assert actor.id is not None
    await _require_team_admin_or_org_admin(actor.id, team_id, session)
    version = _parse_if_match(if_match)
    async with session.begin():
        team = await svc.update_team(
            session, team_id=team_id, payload=payload, if_match_version=version
        )
    return TeamResponse.model_validate(team)


@router.delete("/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team(
    team_id: uuid.UUID,
    _admin: AdminActor,
    session: SessionDep,
) -> None:
    async with session.begin():
        await svc.delete_team(session, team_id=team_id)


@router.post(
    "/{team_id}/members",
    response_model=TeamMembershipResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_member(
    team_id: uuid.UUID,
    payload: TeamMemberAdd,
    actor: UserActor,
    session: SessionDep,
) -> TeamMembershipResponse:
    assert actor.id is not None
    await _require_team_admin_or_org_admin(actor.id, team_id, session)
    async with session.begin():
        m = await svc.add_member(
            session, team_id=team_id, user_id=payload.user_id, role=payload.role
        )
    return TeamMembershipResponse.model_validate(m)


@router.patch("/{team_id}/members/{user_id}", response_model=TeamMembershipResponse)
async def update_member(
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: TeamMemberUpdate,
    actor: UserActor,
    session: SessionDep,
) -> TeamMembershipResponse:
    assert actor.id is not None
    await _require_team_admin_or_org_admin(actor.id, team_id, session)
    async with session.begin():
        m = await svc.update_member_role(
            session, team_id=team_id, user_id=user_id, role=payload.role
        )
    return TeamMembershipResponse.model_validate(m)


@router.delete("/{team_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    actor: UserActor,
    session: SessionDep,
) -> None:
    assert actor.id is not None
    await _require_team_admin_or_org_admin(actor.id, team_id, session)
    async with session.begin():
        await svc.remove_member(session, team_id=team_id, user_id=user_id)
