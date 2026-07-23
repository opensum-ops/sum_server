"""Component read routes (writes happen through agent inventory ingest)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from sum_server.components import service as svc
from sum_server.components.schemas import ComponentKind, ComponentResponse
from sum_server.core.db import SessionDep
from sum_server.core.deps import UserActor
from sum_server.core.errors import ForbiddenError, NotFoundError
from sum_server.hosts.service import get_host, user_can_read
from sum_server.users.service import get_user

router = APIRouter(tags=["components"])


@router.get("/hosts/{host_id}/components", response_model=list[ComponentResponse])
async def list_host_components(
    host_id: uuid.UUID,
    actor: UserActor,
    session: SessionDep,
    kind: Annotated[ComponentKind | None, Query()] = None,
    include_absent: Annotated[bool, Query()] = False,
) -> list[ComponentResponse]:
    assert actor.id is not None
    host = await get_host(session, host_id)
    if host is None:
        raise NotFoundError("host not found")
    user = await get_user(session, actor.id)
    if user is None or (not user.is_admin and not await user_can_read(session, host, actor.id)):
        raise ForbiddenError("not visible")
    rows = await svc.list_components(
        session, host_id=host_id, kind=kind, include_absent=include_absent
    )
    return [ComponentResponse.model_validate(c) for c in rows]


@router.get("/components/{component_id}", response_model=ComponentResponse)
async def get_component(
    component_id: uuid.UUID,
    actor: UserActor,
    session: SessionDep,
) -> ComponentResponse:
    comp = await svc.get_component(session, component_id)
    if comp is None:
        raise NotFoundError("component not found")
    assert actor.id is not None
    host = await get_host(session, comp.host_id)
    if host is None:
        raise NotFoundError("host not found")
    user = await get_user(session, actor.id)
    if user is None or (not user.is_admin and not await user_can_read(session, host, actor.id)):
        raise ForbiddenError("not visible")
    return ComponentResponse.model_validate(comp)
