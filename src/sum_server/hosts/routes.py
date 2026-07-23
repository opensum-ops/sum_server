"""Host routes."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from sum_server.core.db import SessionDep
from sum_server.core.deps import AdminActor, UserActor
from sum_server.core.errors import ForbiddenError, NotFoundError
from sum_server.core.pagination import Cursor, Page, page_params
from sum_server.hosts import service as svc
from sum_server.hosts.schemas import (
    HostCreate,
    HostResponse,
    HostStatus,
    HostUpdate,
    HostWithOwnersResponse,
    OwnerAddRequest,
)
from sum_server.users.service import get_user

router = APIRouter(prefix="/hosts", tags=["hosts"])


def _parse_if_match(if_match: str | None) -> int | None:
    if if_match is None:
        return None
    try:
        return int(if_match.strip().strip('"'))
    except ValueError as exc:
        raise ForbiddenError("invalid If-Match") from exc


async def _require_owner_or_admin(
    host_id: uuid.UUID, actor_id: uuid.UUID, session: AsyncSession
) -> None:
    try:
        user = await get_user(session, actor_id)
        if user is not None and user.is_admin:
            return
        host = await svc.get_host(session, host_id)
        if host is None:
            raise NotFoundError("host not found")
        if not await svc.user_can_read(session, host, actor_id):
            raise ForbiddenError("not an owner")
    finally:
        # Release the auto-begun read transaction so the handler owns its own
        # ``async with session.begin()``.
        await session.rollback()


@router.post("", response_model=HostResponse, status_code=status.HTTP_201_CREATED)
async def create_host(
    payload: HostCreate,
    _admin: AdminActor,
    session: SessionDep,
) -> HostResponse:
    async with session.begin():
        host = await svc.create_host(session, payload)
    return HostResponse.model_validate(host)


@router.get("", response_model=Page[HostResponse])
async def list_hosts(
    actor: UserActor,
    session: SessionDep,
    pagination: Annotated[tuple[int, object], Depends(page_params)],
    status_filter: Annotated[HostStatus | None, Query(alias="status")] = None,
) -> Page[HostResponse]:
    limit, cursor = pagination
    assert actor.id is not None
    rows = await svc.list_hosts_visible_to(
        session,
        actor_user_id=actor.id,
        limit=limit,
        cursor=cursor,  # type: ignore[arg-type]
        status_filter=status_filter,
    )
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = (
        Cursor(id=items[-1].id, ts=items[-1].created_at).encode() if (has_more and items) else None
    )
    return Page[HostResponse](
        items=[HostResponse.model_validate(s) for s in items], next_cursor=next_cursor
    )


@router.get("/{host_id}", response_model=HostWithOwnersResponse)
async def get_host(
    host_id: uuid.UUID,
    actor: UserActor,
    session: SessionDep,
) -> HostWithOwnersResponse:
    host = await svc.get_host(session, host_id)
    if host is None:
        raise NotFoundError("host not found")
    assert actor.id is not None
    if not await svc.user_can_read(session, host, actor.id):
        raise ForbiddenError("not visible")
    resp = HostWithOwnersResponse.model_validate(host)
    resp.user_owners = await svc.get_user_owner_ids(session, host_id)
    resp.team_owners = await svc.get_team_owner_ids(session, host_id)
    return resp


@router.patch("/{host_id}", response_model=HostResponse)
async def update_host(
    host_id: uuid.UUID,
    payload: HostUpdate,
    actor: UserActor,
    session: SessionDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> HostResponse:
    assert actor.id is not None
    await _require_owner_or_admin(host_id, actor.id, session)
    version = _parse_if_match(if_match)
    async with session.begin():
        host = await svc.update_host(
            session, host_id=host_id, payload=payload, if_match_version=version
        )
    return HostResponse.model_validate(host)


@router.delete("/{host_id}", status_code=status.HTTP_204_NO_CONTENT)
async def decommission_host(
    host_id: uuid.UUID,
    _admin: AdminActor,
    session: SessionDep,
) -> None:
    async with session.begin():
        await svc.decommission_host(session, host_id=host_id)


@router.post("/{host_id}/owners", status_code=status.HTTP_204_NO_CONTENT)
async def add_owner(
    host_id: uuid.UUID,
    payload: OwnerAddRequest,
    actor: UserActor,
    session: SessionDep,
) -> None:
    assert actor.id is not None
    await _require_owner_or_admin(host_id, actor.id, session)
    async with session.begin():
        if payload.user_id is not None:
            await svc.add_user_owner(session, host_id=host_id, user_id=payload.user_id)
        else:
            assert payload.team_id is not None
            await svc.add_team_owner(session, host_id=host_id, team_id=payload.team_id)


@router.delete("/{host_id}/owners/user/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_user_owner(
    host_id: uuid.UUID,
    user_id: uuid.UUID,
    actor: UserActor,
    session: SessionDep,
) -> None:
    assert actor.id is not None
    await _require_owner_or_admin(host_id, actor.id, session)
    async with session.begin():
        await svc.remove_user_owner(session, host_id=host_id, user_id=user_id)


@router.delete("/{host_id}/owners/team/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_team_owner(
    host_id: uuid.UUID,
    team_id: uuid.UUID,
    actor: UserActor,
    session: SessionDep,
) -> None:
    assert actor.id is not None
    await _require_owner_or_admin(host_id, actor.id, session)
    async with session.begin():
        await svc.remove_team_owner(session, host_id=host_id, team_id=team_id)
