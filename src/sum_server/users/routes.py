"""User routes."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, status

from sum_server.core.db import SessionDep
from sum_server.core.deps import AdminActor, UserActor
from sum_server.core.errors import ForbiddenError, NotFoundError
from sum_server.core.pagination import Page, page_params
from sum_server.users import service as svc
from sum_server.users.schemas import UserCreate, UserResponse, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate,
    _admin: AdminActor,
    session: SessionDep,
) -> UserResponse:
    async with session.begin():
        user = await svc.create_user(session, payload)
    return UserResponse.model_validate(user)


@router.get("", response_model=Page[UserResponse])
async def list_users(
    _admin: AdminActor,
    session: SessionDep,
    pagination: Annotated[tuple[int, object], Depends(page_params)],
    include_deleted: bool = False,
) -> Page[UserResponse]:
    limit, cursor = pagination
    rows = await svc.list_users(
        session,
        limit=limit,
        cursor=cursor,  # type: ignore[arg-type]
        include_deleted=include_deleted,
    )
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = None
    if has_more and items:
        from sum_server.core.pagination import Cursor

        last = items[-1]
        next_cursor = Cursor(id=last.id, ts=last.created_at).encode()
    return Page[UserResponse](
        items=[UserResponse.model_validate(u) for u in items], next_cursor=next_cursor
    )


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: uuid.UUID,
    actor: UserActor,
    session: SessionDep,
) -> UserResponse:
    target = await svc.get_user(session, user_id)
    if target is None or target.deleted_at is not None:
        raise NotFoundError("user not found")
    assert actor.id is not None
    if actor.id != target.id:
        # Only admins can read others.
        me = await svc.get_user(session, actor.id)
        if me is None or not me.is_admin:
            raise ForbiddenError("cannot read other users")
    return UserResponse.model_validate(target)


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    actor: UserActor,
    session: SessionDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> UserResponse:
    assert actor.id is not None
    me = await svc.get_user(session, actor.id)
    if me is None:
        raise NotFoundError("user not found")
    by_admin = bool(me.is_admin)
    if actor.id != user_id and not by_admin:
        raise ForbiddenError("cannot update other users")
    if payload.is_admin is not None and not by_admin:
        raise ForbiddenError("only admins can change is_admin")

    version: int | None = None
    if if_match is not None:
        try:
            version = int(if_match.strip().strip('"'))
        except ValueError as exc:
            raise ForbiddenError("invalid If-Match") from exc

    async with session.begin():
        user = await svc.update_user(
            session, user_id=user_id, payload=payload, by_admin=by_admin, if_match_version=version
        )
    return UserResponse.model_validate(user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID,
    _admin: AdminActor,
    session: SessionDep,
) -> None:
    async with session.begin():
        await svc.delete_user(session, user_id=user_id)
