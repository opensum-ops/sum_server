"""Group routes: tree CRUD, memberships, parameters.

Reads are open to any authenticated user (group structure is not secret);
all mutations and the member list are admin-only.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from sum_server.core.db import SessionDep
from sum_server.core.deps import AdminActor, UserActor
from sum_server.core.errors import InvalidInputError, NotFoundError
from sum_server.groups import service as svc
from sum_server.groups.schemas import (
    GroupCreate,
    GroupResponse,
    GroupUpdate,
    MemberAddRequest,
    ParameterResponse,
    ParameterSet,
    validate_param_key,
)

router = APIRouter(prefix="/groups", tags=["groups"])


def checked_param_key(key: str) -> str:
    """Validate a path-supplied parameter key (422 on failure)."""
    try:
        return validate_param_key(key)
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc


@router.get("", response_model=list[GroupResponse])
async def list_groups(_actor: UserActor, session: SessionDep) -> list[GroupResponse]:
    return [GroupResponse.model_validate(g) for g in await svc.list_groups(session)]


@router.post("", response_model=GroupResponse, status_code=status.HTTP_201_CREATED)
async def create_group(
    payload: GroupCreate,
    _admin: AdminActor,
    session: SessionDep,
) -> GroupResponse:
    async with session.begin():
        group = await svc.create_group(
            session,
            name=payload.name,
            description=payload.description,
            parent_id=payload.parent_id,
        )
    return GroupResponse.model_validate(group)


@router.get("/{group_id}", response_model=GroupResponse)
async def get_group(
    group_id: uuid.UUID,
    _actor: UserActor,
    session: SessionDep,
) -> GroupResponse:
    group = await svc.get_group(session, group_id)
    if group is None:
        raise NotFoundError("group not found")
    return GroupResponse.model_validate(group)


@router.patch("/{group_id}", response_model=GroupResponse)
async def update_group(
    group_id: uuid.UUID,
    payload: GroupUpdate,
    _admin: AdminActor,
    session: SessionDep,
) -> GroupResponse:
    async with session.begin():
        group = await svc.update_group(
            session,
            group_id=group_id,
            name=payload.name,
            description=payload.description,
            parent_id=payload.parent_id,
        )
    return GroupResponse.model_validate(group)


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    group_id: uuid.UUID,
    _admin: AdminActor,
    session: SessionDep,
) -> None:
    async with session.begin():
        await svc.delete_group(session, group_id=group_id)


# Membership ---------------------------------------------------------------


@router.get("/{group_id}/members", response_model=list[uuid.UUID])
async def list_members(
    group_id: uuid.UUID,
    _admin: AdminActor,
    session: SessionDep,
    include_descendants: bool = False,
) -> list[uuid.UUID]:
    """Direct members by default; the whole subtree when asked.

    Direct membership stays the default because it is what the add and remove
    endpoints act on, and changing what an existing response means would break
    a caller that had no say in it.
    """
    if await svc.get_group(session, group_id) is None:
        raise NotFoundError("group not found")
    if include_descendants:
        return await svc.list_effective_member_host_ids(session, group_id=group_id)
    return await svc.list_member_host_ids(session, group_id=group_id)


@router.post("/{group_id}/members", status_code=status.HTTP_204_NO_CONTENT)
async def add_member(
    group_id: uuid.UUID,
    payload: MemberAddRequest,
    _admin: AdminActor,
    session: SessionDep,
) -> None:
    async with session.begin():
        await svc.add_member(session, group_id=group_id, host_id=payload.host_id)


@router.delete("/{group_id}/members/{host_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    group_id: uuid.UUID,
    host_id: uuid.UUID,
    _admin: AdminActor,
    session: SessionDep,
) -> None:
    async with session.begin():
        await svc.remove_member(session, group_id=group_id, host_id=host_id)


# Parameters ---------------------------------------------------------------


@router.get("/{group_id}/parameters", response_model=list[ParameterResponse])
async def list_parameters(
    group_id: uuid.UUID,
    _actor: UserActor,
    session: SessionDep,
) -> list[ParameterResponse]:
    if await svc.get_group(session, group_id) is None:
        raise NotFoundError("group not found")
    rows = await svc.list_group_parameters(session, group_id=group_id)
    return [ParameterResponse.model_validate(r) for r in rows]


@router.put("/{group_id}/parameters/{key}", response_model=ParameterResponse)
async def set_parameter(
    group_id: uuid.UUID,
    key: str,
    payload: ParameterSet,
    _admin: AdminActor,
    session: SessionDep,
) -> ParameterResponse:
    key = checked_param_key(key)
    async with session.begin():
        row = await svc.set_group_parameter(
            session, group_id=group_id, key=key, value=payload.value
        )
    return ParameterResponse.model_validate(row)


@router.delete("/{group_id}/parameters/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def unset_parameter(
    group_id: uuid.UUID,
    key: str,
    _admin: AdminActor,
    session: SessionDep,
) -> None:
    async with session.begin():
        found = await svc.unset_group_parameter(session, group_id=group_id, key=key)
    if not found:
        raise NotFoundError("parameter not found")
