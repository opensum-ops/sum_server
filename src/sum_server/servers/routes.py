"""Server routes."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from sum_server.core.db import SessionDep
from sum_server.core.deps import AdminActor, UserActor
from sum_server.core.errors import ForbiddenError, NotFoundError
from sum_server.core.pagination import Cursor, Page, page_params
from sum_server.servers import service as svc
from sum_server.servers.schemas import (
    OwnerAddRequest,
    ServerCreate,
    ServerResponse,
    ServerStatus,
    ServerUpdate,
    ServerWithOwnersResponse,
)
from sum_server.users.service import get_user

router = APIRouter(prefix="/servers", tags=["servers"])


def _parse_if_match(if_match: str | None) -> int | None:
    if if_match is None:
        return None
    try:
        return int(if_match.strip().strip('"'))
    except ValueError as exc:
        raise ForbiddenError("invalid If-Match") from exc


async def _require_owner_or_admin(
    server_id: uuid.UUID, actor_id: uuid.UUID, session: AsyncSession
) -> None:
    try:
        user = await get_user(session, actor_id)
        if user is not None and user.is_admin:
            return
        server = await svc.get_server(session, server_id)
        if server is None:
            raise NotFoundError("server not found")
        if not await svc.user_can_read(session, server, actor_id):
            raise ForbiddenError("not an owner")
    finally:
        # Release the auto-begun read transaction so the handler owns its own
        # ``async with session.begin()``.
        await session.rollback()


@router.post("", response_model=ServerResponse, status_code=status.HTTP_201_CREATED)
async def create_server(
    payload: ServerCreate,
    _admin: AdminActor,
    session: SessionDep,
) -> ServerResponse:
    async with session.begin():
        server = await svc.create_server(session, payload)
    return ServerResponse.model_validate(server)


@router.get("", response_model=Page[ServerResponse])
async def list_servers(
    actor: UserActor,
    session: SessionDep,
    pagination: Annotated[tuple[int, object], Depends(page_params)],
    status_filter: Annotated[ServerStatus | None, Query(alias="status")] = None,
) -> Page[ServerResponse]:
    limit, cursor = pagination
    assert actor.id is not None
    rows = await svc.list_servers_visible_to(
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
    return Page[ServerResponse](
        items=[ServerResponse.model_validate(s) for s in items], next_cursor=next_cursor
    )


@router.get("/{server_id}", response_model=ServerWithOwnersResponse)
async def get_server(
    server_id: uuid.UUID,
    actor: UserActor,
    session: SessionDep,
) -> ServerWithOwnersResponse:
    server = await svc.get_server(session, server_id)
    if server is None:
        raise NotFoundError("server not found")
    assert actor.id is not None
    if not await svc.user_can_read(session, server, actor.id):
        raise ForbiddenError("not visible")
    resp = ServerWithOwnersResponse.model_validate(server)
    resp.user_owners = await svc.get_user_owner_ids(session, server_id)
    resp.team_owners = await svc.get_team_owner_ids(session, server_id)
    return resp


@router.patch("/{server_id}", response_model=ServerResponse)
async def update_server(
    server_id: uuid.UUID,
    payload: ServerUpdate,
    actor: UserActor,
    session: SessionDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> ServerResponse:
    assert actor.id is not None
    await _require_owner_or_admin(server_id, actor.id, session)
    version = _parse_if_match(if_match)
    async with session.begin():
        server = await svc.update_server(
            session, server_id=server_id, payload=payload, if_match_version=version
        )
    return ServerResponse.model_validate(server)


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def decommission_server(
    server_id: uuid.UUID,
    _admin: AdminActor,
    session: SessionDep,
) -> None:
    async with session.begin():
        await svc.decommission_server(session, server_id=server_id)


@router.post("/{server_id}/owners", status_code=status.HTTP_204_NO_CONTENT)
async def add_owner(
    server_id: uuid.UUID,
    payload: OwnerAddRequest,
    actor: UserActor,
    session: SessionDep,
) -> None:
    assert actor.id is not None
    await _require_owner_or_admin(server_id, actor.id, session)
    async with session.begin():
        if payload.user_id is not None:
            await svc.add_user_owner(session, server_id=server_id, user_id=payload.user_id)
        else:
            assert payload.team_id is not None
            await svc.add_team_owner(session, server_id=server_id, team_id=payload.team_id)


@router.delete("/{server_id}/owners/user/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_user_owner(
    server_id: uuid.UUID,
    user_id: uuid.UUID,
    actor: UserActor,
    session: SessionDep,
) -> None:
    assert actor.id is not None
    await _require_owner_or_admin(server_id, actor.id, session)
    async with session.begin():
        await svc.remove_user_owner(session, server_id=server_id, user_id=user_id)


@router.delete("/{server_id}/owners/team/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_team_owner(
    server_id: uuid.UUID,
    team_id: uuid.UUID,
    actor: UserActor,
    session: SessionDep,
) -> None:
    assert actor.id is not None
    await _require_owner_or_admin(server_id, actor.id, session)
    async with session.begin():
        await svc.remove_team_owner(session, server_id=server_id, team_id=team_id)
