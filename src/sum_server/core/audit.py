"""Audit log model + write helper.

Audit entries are append-only. SQLAlchemy event listeners reject UPDATE/DELETE on
:class:`AuditEntry` as defense in depth; production should additionally GRANT only
INSERT/SELECT on this table to the application Postgres role.

:func:`write_audit` reads the current actor from contextvars and writes within
the caller's open session so audit and state changes commit together.
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import Index, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from sum_server.core.context import ActorKind, get_actor
from sum_server.core.db import Base, IdMixin
from sum_server.core.ids import new_id

class AuditEntry(Base, IdMixin):
    __tablename__ = "audit_entries"

    ts: Mapped[dt.datetime] = mapped_column(
        default=lambda: dt.datetime.now(tz=dt.UTC), nullable=False
    )
    actor_kind: Mapped[str] = mapped_column(nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    action: Mapped[str] = mapped_column(nullable=False)
    target_kind: Mapped[str] = mapped_column(nullable=False)
    target_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_audit_entries_ts", "ts"),
        Index("ix_audit_entries_actor", "actor_kind", "actor_id"),
        Index("ix_audit_entries_target", "target_kind", "target_id"),
        Index("ix_audit_entries_action", "action"),
    )

async def write_audit(
    session: AsyncSession,
    *,
    action: str,
    target_kind: str,
    target_id: uuid.UUID | None,
    payload: dict[str, Any] | None = None,
    actor_kind: ActorKind | None = None,
    actor_id: uuid.UUID | None = None,
) -> AuditEntry:
    """Persist an audit row attributed to the current actor (unless overridden).
    
    The caller's session/transaction is reused so the entry commits with the
    state change. If either fails, both roll back.
    """
    if actor_kind is None or actor_id is None:
        actor = get_actor()
        actor_kind = actor_kind or actor.kind
        actor_id = actor_id if actor_id is not None else actor.id
    entry = AuditEntry(
        id = new_id(),
        actor_kind = actor_kind,
        actor_id = actor_id,
        action = action,
        target_kind = target_kind,
        target_id = target_id,
        payload = payload or {},
    )
    session.add(entry)
    return entry

def _block_modification(_mapper: Any, _connection: Any, _target: Any) -> None:
    raise InvalidRequestError("AuditEntry is append-only")

event.listen(AuditEntry, "before_update", _block_modification)
event.listen(AuditEntry, "before_delete", _block_modification)