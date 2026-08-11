"""Recording and reading host change history."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sum_server.core.context import get_actor
from sum_server.core.ids import new_id
from sum_server.history.models import HostChange


def _utcnow() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def record(
    session: AsyncSession,
    *,
    host_id: uuid.UUID,
    scope: str,
    field: str,
    change: str,
    old: Any | None = None,
    new: Any | None = None,
    component_kind: str | None = None,
    subject_id: uuid.UUID | None = None,
    subject_label: str | None = None,
    at: dt.datetime | None = None,
) -> HostChange:
    """Append one change to the caller's open session.

    Same contract as :func:`~sum_server.core.audit.write_audit`: the actor comes
    from the request contextvar and the row joins the caller's transaction, so a
    change and the state it describes commit or roll back together.
    """
    actor = get_actor()
    entry = HostChange(
        id=new_id(),
        host_id=host_id,
        observed_at=at or _utcnow(),
        scope=scope,
        component_kind=component_kind,
        subject_id=subject_id,
        subject_label=subject_label,
        field=field,
        change=change,
        old_value=old,
        new_value=new,
        actor_kind=actor.kind,
        actor_id=actor.id,
    )
    session.add(entry)
    return entry


def _scope_clause(scope: str | Sequence[str]) -> ColumnElement[bool]:
    """One scope or several. The Groups pane spans ``group`` and ``param``."""
    if isinstance(scope, str):
        return HostChange.scope == scope
    return HostChange.scope.in_(list(scope))


async def changes(
    session: AsyncSession,
    *,
    host_id: uuid.UUID,
    scope: str | Sequence[str] | None = None,
    field: str | None = None,
    subject_id: uuid.UUID | None = None,
    component_kind: str | None = None,
    limit: int = 50,
) -> list[HostChange]:
    """Newest-first change list, narrowed by whichever filters are given."""
    stmt = select(HostChange).where(HostChange.host_id == host_id)
    if scope is not None:
        stmt = stmt.where(_scope_clause(scope))
    if field is not None:
        stmt = stmt.where(HostChange.field == field)
    if subject_id is not None:
        stmt = stmt.where(HostChange.subject_id == subject_id)
    if component_kind is not None:
        stmt = stmt.where(HostChange.component_kind == component_kind)
    stmt = stmt.order_by(HostChange.observed_at.desc(), HostChange.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def field_counts(session: AsyncSession, *, host_id: uuid.UUID, scope: str) -> dict[str, int]:
    """``field -> change count`` for one scope, in a single query.

    The page renders a control per row; without this it would issue one count
    per field on every render.
    """
    rows = (
        await session.execute(
            select(HostChange.field, func.count())
            .where(HostChange.host_id == host_id, HostChange.scope == scope)
            .group_by(HostChange.field)
        )
    ).tuples()
    return dict(rows.all())


async def subject_counts(session: AsyncSession, *, host_id: uuid.UUID) -> dict[uuid.UUID, int]:
    """``component id -> change count``, for the per-row controls in tables."""
    rows = (
        await session.execute(
            select(HostChange.subject_id, func.count())
            .where(HostChange.host_id == host_id, HostChange.subject_id.isnot(None))
            .group_by(HostChange.subject_id)
        )
    ).all()
    return {subject_id: count for subject_id, count in rows if subject_id is not None}


async def scope_count(
    session: AsyncSession,
    *,
    host_id: uuid.UUID,
    scope: str | Sequence[str] | None = None,
    component_kind: str | None = None,
) -> int:
    """Total changes for a pane, for its "Changes (N)" summary."""
    stmt = select(func.count()).select_from(HostChange).where(HostChange.host_id == host_id)
    if scope is not None:
        stmt = stmt.where(_scope_clause(scope))
    if component_kind is not None:
        stmt = stmt.where(HostChange.component_kind == component_kind)
    return int((await session.execute(stmt)).scalar_one())
