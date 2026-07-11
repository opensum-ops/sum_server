"""Audit read routes (admin only). Writes happen via ``core.audit.write_audit``."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from sum_server.audit.schemas import AuditEntryResponse
from sum_server.core.audit import AuditEntry
from sum_server.core.db import SessionDep
from sum_server.core.deps import AdminActor
from sum_server.core.errors import NotFoundError
from sum_server.core.pagination import Cursor, Page, page_params

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=Page[AuditEntryResponse])
async def list_audit(
    _admin: AdminActor,
    session: SessionDep,
    pagination: Annotated[tuple[int, object], Depends(page_params)],
    actor_kind: Annotated[str | None, Query()] = None,
    actor_id: Annotated[uuid.UUID | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
    target_kind: Annotated[str | None, Query()] = None,
    target_id: Annotated[uuid.UUID | None, Query()] = None,
    since: Annotated[dt.datetime | None, Query()] = None,
    until: Annotated[dt.datetime | None, Query()] = None,
) -> Page[AuditEntryResponse]:
    limit, cursor = pagination
    stmt = select(AuditEntry)
    if actor_kind is not None:
        stmt = stmt.where(AuditEntry.actor_kind == actor_kind)
    if actor_id is not None:
        stmt = stmt.where(AuditEntry.actor_id == actor_id)
    if action is not None:
        stmt = stmt.where(AuditEntry.action == action)
    if target_kind is not None:
        stmt = stmt.where(AuditEntry.target_kind == target_kind)
    if target_id is not None:
        stmt = stmt.where(AuditEntry.target_id == target_id)
    if since is not None:
        stmt = stmt.where(AuditEntry.ts >= since)
    if until is not None:
        stmt = stmt.where(AuditEntry.ts < until)
    if cursor is not None:
        c: Cursor = cursor  # type: ignore[assignment]
        stmt = stmt.where(
            (AuditEntry.ts, AuditEntry.id) < (c.ts, c.id)  # type: ignore[operator]
        )
    stmt = stmt.order_by(AuditEntry.ts.desc(), AuditEntry.id.desc()).limit(limit + 1)
    rows = list((await session.execute(stmt)).scalars().all())
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = (
        Cursor(id=items[-1].id, ts=items[-1].ts).encode() if (has_more and items) else None
    )
    return Page[AuditEntryResponse](
        items=[AuditEntryResponse.model_validate(r) for r in items],
        next_cursor=next_cursor,
    )


@router.get("/{entry_id}", response_model=AuditEntryResponse)
async def get_audit_entry(
    entry_id: uuid.UUID,
    _admin: AdminActor,
    session: SessionDep,
) -> AuditEntryResponse:
    row = (
        await session.execute(select(AuditEntry).where(AuditEntry.id == entry_id))
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("audit entry not found")
    return AuditEntryResponse.model_validate(row)
