"""Component services: read + inventory ingest with disk-swap detection."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sum_server.components.models import Component
from sum_server.components.schemas import ComponentIngest
from sum_server.core.audit import write_audit
from sum_server.core.errors import NotFoundError
from sum_server.core.ids import new_id


def _utcnow() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


async def get_component(session: AsyncSession, component_id: uuid.UUID) -> Component | None:
    return (
        await session.execute(select(Component).where(Component.id == component_id))
    ).scalar_one_or_none()


async def list_components(
    session: AsyncSession,
    *,
    host_id: uuid.UUID,
    kind: str | None = None,
    include_absent: bool = False,
) -> list[Component]:
    stmt = select(Component).where(Component.host_id == host_id)
    if kind is not None:
        stmt = stmt.where(Component.kind == kind)
    if not include_absent:
        stmt = stmt.where(Component.present.is_(True))
    stmt = stmt.order_by(Component.kind, Component.slot, Component.serial)
    return list((await session.execute(stmt)).scalars().all())


async def _find_by_identity(
    session: AsyncSession, *, host_id: uuid.UUID, kind: str, serial: str | None, slot: str | None
) -> Component | None:
    """Identity preference: ``(host, kind, serial)`` if serial present,
    else ``(host, kind, slot)``."""
    if serial is not None:
        return (
            await session.execute(
                select(Component).where(
                    Component.host_id == host_id,
                    Component.kind == kind,
                    Component.serial == serial,
                )
            )
        ).scalar_one_or_none()
    if slot is not None:
        return (
            await session.execute(
                select(Component).where(
                    Component.host_id == host_id,
                    Component.kind == kind,
                    Component.serial.is_(None),
                    Component.slot == slot,
                )
            )
        ).scalar_one_or_none()
    return None


async def _find_slot_swap(
    session: AsyncSession,
    *,
    host_id: uuid.UUID,
    kind: str,
    slot: str | None,
    serial: str | None,
) -> Component | None:
    """Return any existing present component at the same
    ``(host, kind, slot)`` with a different serial."""
    if serial is None or slot is None:
        return None
    return (
        await session.execute(
            select(Component).where(
                Component.host_id == host_id,
                Component.kind == kind,
                Component.slot == slot,
                Component.serial.isnot(None),
                Component.serial != serial,
                Component.present.is_(True),
            )
        )
    ).scalar_one_or_none()


async def ingest_inventory(
    session: AsyncSession,
    *,
    host_id: uuid.UUID,
    entries: Sequence[ComponentIngest],
) -> dict[str, int]:
    """Upsert an inventory snapshot for a host.

    Components not mentioned in the snapshot are marked ``present=false``. Serial
    changes at the same slot emit a ``host.component_swap`` audit event and
    the old component is marked absent.
    """
    from sum_server.hosts.service import get_host

    host = await get_host(session, host_id)
    if host is None:
        raise NotFoundError("host not found")

    now = _utcnow()
    counts = {"created": 0, "updated": 0, "marked_absent": 0, "swaps": 0}
    seen_ids: set[uuid.UUID] = set()

    for entry in entries:
        swap_target = await _find_slot_swap(
            session,
            host_id=host_id,
            kind=entry.kind,
            slot=entry.slot,
            serial=entry.serial,
        )
        if swap_target is not None:
            swap_target.present = False
            swap_target.last_seen = now
            await write_audit(
                session,
                action="host.component_swap",
                target_kind="host",
                target_id=host_id,
                payload={
                    "kind": entry.kind,
                    "slot": entry.slot,
                    "old_serial": swap_target.serial,
                    "new_serial": entry.serial,
                },
            )
            counts["swaps"] += 1
        existing = await _find_by_identity(
            session,
            host_id=host_id,
            kind=entry.kind,
            serial=entry.serial,
            slot=entry.slot,
        )
        if existing is None:
            comp = Component(
                id=new_id(),
                host_id=host_id,
                kind=entry.kind,
                vendor=entry.vendor,
                model=entry.model,
                serial=entry.serial,
                slot=entry.slot,
                present=True,
                attrs=entry.attrs.model_dump(),
                first_seen=now,
                last_seen=now,
            )
            session.add(comp)
            await session.flush()
            seen_ids.add(comp.id)
            counts["created"] += 1
        else:
            existing.vendor = entry.vendor
            existing.model = entry.model
            existing.slot = entry.slot
            existing.attrs = entry.attrs.model_dump()
            existing.present = True
            existing.last_seen = now
            seen_ids.add(existing.id)
            counts["updated"] += 1

    # Mark anything previously present but not in this snapshot as absent.
    all_present = (
        (
            await session.execute(
                select(Component).where(Component.host_id == host_id, Component.present.is_(True))
            )
        )
        .scalars()
        .all()
    )
    for c in all_present:
        if c.id not in seen_ids:
            c.present = False
            c.last_seen = now
            counts["marked_absent"] += 1

    await write_audit(
        session,
        action="agent.inventory_submitted",
        target_kind="host",
        target_id=host_id,
        payload=counts,
    )
    return counts
