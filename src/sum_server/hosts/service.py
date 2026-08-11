"""Host services: CRUD, ownership (M:N users + teams), visibility scoping."""

from __future__ import annotations

import uuid

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sum_server.core.audit import write_audit
from sum_server.core.errors import (
    ConflictError,
    NotFoundError,
    PreconditionFailedError,
)
from sum_server.core.ids import new_id
from sum_server.core.pagination import Cursor
from sum_server.history import service as history
from sum_server.hosts.models import (
    Host,
    host_owner_teams,
    host_owner_users,
)
from sum_server.hosts.schemas import HostCreate, HostUpdate


async def get_host(session: AsyncSession, host_id: uuid.UUID) -> Host | None:
    return (await session.execute(select(Host).where(Host.id == host_id))).scalar_one_or_none()


async def get_user_owner_ids(session: AsyncSession, host_id: uuid.UUID) -> list[uuid.UUID]:
    rows = (
        (
            await session.execute(
                select(host_owner_users.c.user_id).where(host_owner_users.c.host_id == host_id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def get_team_owner_ids(session: AsyncSession, host_id: uuid.UUID) -> list[uuid.UUID]:
    rows = (
        (
            await session.execute(
                select(host_owner_teams.c.team_id).where(host_owner_teams.c.host_id == host_id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def user_can_read(session: AsyncSession, host: Host, user_id: uuid.UUID) -> bool:
    """True if user is an admin, a direct owner, or a member of an owning team."""
    from sum_server.users.service import get_user

    user = await get_user(session, user_id)
    if user is None:
        return False
    if user.is_admin:
        return True
    direct = (
        await session.execute(
            select(func.count())
            .select_from(host_owner_users)
            .where(
                host_owner_users.c.host_id == host.id,
                host_owner_users.c.user_id == user_id,
            )
        )
    ).scalar_one()
    if direct:
        return True

    from sum_server.teams.models import TeamMembership

    via_team = (
        await session.execute(
            select(func.count())
            .select_from(host_owner_teams)
            .join(
                TeamMembership,
                and_(
                    TeamMembership.team_id == host_owner_teams.c.team_id,
                    TeamMembership.user_id == user_id,
                ),
            )
            .where(host_owner_teams.c.host_id == host.id)
        )
    ).scalar_one()
    return bool(via_team)


async def list_hosts_visible_to(
    session: AsyncSession,
    *,
    actor_user_id: uuid.UUID,
    limit: int,
    cursor: Cursor | None,
    status_filter: str | None = None,
) -> list[Host]:
    from sum_server.teams.models import TeamMembership
    from sum_server.users.service import get_user

    user = await get_user(session, actor_user_id)
    if user is None:
        return []
    stmt = select(Host)
    if not user.is_admin:
        stmt = stmt.where(
            or_(
                Host.id.in_(
                    select(host_owner_users.c.host_id).where(
                        host_owner_users.c.user_id == actor_user_id
                    )
                ),
                Host.id.in_(
                    select(host_owner_teams.c.host_id)
                    .join(
                        TeamMembership,
                        TeamMembership.team_id == host_owner_teams.c.team_id,
                    )
                    .where(TeamMembership.user_id == actor_user_id)
                ),
            )
        )
    if status_filter is not None:
        stmt = stmt.where(Host.status == status_filter)
    if cursor is not None:
        stmt = stmt.where(
            (Host.created_at, Host.id) < (cursor.ts, cursor.id)  # type: ignore[operator]
        )
    stmt = stmt.order_by(Host.created_at.desc(), Host.id.desc()).limit(limit + 1)
    return list((await session.execute(stmt)).scalars().all())


async def create_host(session: AsyncSession, payload: HostCreate) -> Host:
    if payload.status not in ("provisioning", "active"):
        raise ConflictError("new hosts must be provisioning or active")
    host = Host(
        id=new_id(),
        hostname=payload.hostname.strip(),
        description=payload.description,
        status=payload.status,
    )
    session.add(host)
    await session.flush()
    await write_audit(
        session,
        action="host.create",
        target_kind="host",
        target_id=host.id,
        payload={"hostname": host.hostname, "status": host.status},
    )
    return host


async def update_host(
    session: AsyncSession,
    *,
    host_id: uuid.UUID,
    payload: HostUpdate,
    if_match_version: int | None = None,
) -> Host:
    host = await get_host(session, host_id)
    if host is None:
        raise NotFoundError("host not found")
    if if_match_version is not None and host.version != if_match_version:
        raise PreconditionFailedError("host has been modified")
    if host.status == "decommissioned":
        raise ConflictError("host is decommissioned; cannot modify")
    changed: dict[str, object] = {}
    if payload.hostname is not None and payload.hostname.strip() != host.hostname:
        history.record(
            session,
            host_id=host.id,
            scope="host",
            field="hostname",
            change="edit",
            old=host.hostname,
            new=payload.hostname.strip(),
        )
        host.hostname = payload.hostname.strip()
        changed["hostname"] = host.hostname
    if payload.description is not None:
        # "" is how a form clears the field; store it as absent, not as an
        # empty string, so the column has one representation of "unset".
        description = payload.description.strip() or None
        if description != host.description:
            history.record(
                session,
                host_id=host.id,
                scope="host",
                field="description",
                change="add" if host.description is None else "edit",
                old=host.description,
                new=description,
            )
            host.description = description
            changed["description"] = host.description
    if payload.status is not None and payload.status != host.status:
        if payload.status == "decommissioned":
            raise ConflictError("use DELETE to decommission a host")
        history.record(
            session,
            host_id=host.id,
            scope="host",
            field="status",
            change="edit",
            old=host.status,
            new=payload.status,
        )
        host.status = payload.status
        changed["status"] = payload.status
    if changed:
        await write_audit(
            session,
            action="host.update",
            target_kind="host",
            target_id=host.id,
            payload={"changed": list(changed.keys())},
        )
    return host


async def decommission_host(session: AsyncSession, *, host_id: uuid.UUID) -> Host:
    host = await get_host(session, host_id)
    if host is None:
        raise NotFoundError("host not found")
    if host.status == "decommissioned":
        return host
    host.status = "decommissioned"
    await write_audit(
        session,
        action="host.decommission",
        target_kind="host",
        target_id=host_id,
        payload={},
    )
    return host


async def add_user_owner(
    session: AsyncSession,
    *,
    host_id: uuid.UUID,
    user_id: uuid.UUID,
) -> bool:
    host = await get_host(session, host_id)
    if host is None:
        raise NotFoundError("host not found")
    if host.status == "decommissioned":
        raise ConflictError("host is decommissioned; ownership is frozen")
    from sum_server.users.service import get_user

    user = await get_user(session, user_id)
    if user is None or user.deleted_at is not None:
        raise NotFoundError("user not found")
    existing = (
        await session.execute(
            select(func.count())
            .select_from(host_owner_users)
            .where(
                host_owner_users.c.host_id == host_id,
                host_owner_users.c.user_id == user_id,
            )
        )
    ).scalar_one()
    if existing:
        await write_audit(
            session,
            action="host.add_owner",
            target_kind="host",
            target_id=host_id,
            payload={"owner_kind": "user", "owner_id": str(user_id), "noop": True},
        )
        return False
    await session.execute(host_owner_users.insert().values(host_id=host_id, user_id=user_id))
    await write_audit(
        session,
        action="host.add_owner",
        target_kind="host",
        target_id=host_id,
        payload={"owner_kind": "user", "owner_id": str(user_id)},
    )
    return True


async def add_team_owner(
    session: AsyncSession,
    *,
    host_id: uuid.UUID,
    team_id: uuid.UUID,
) -> bool:
    host = await get_host(session, host_id)
    if host is None:
        raise NotFoundError("host not found")
    if host.status == "decommissioned":
        raise ConflictError("host is decommissioned; ownership is frozen")
    from sum_server.teams.service import get_team

    team = await get_team(session, team_id)
    if team is None:
        raise NotFoundError("team not found")
    existing = (
        await session.execute(
            select(func.count())
            .select_from(host_owner_teams)
            .where(
                host_owner_teams.c.host_id == host_id,
                host_owner_teams.c.team_id == team_id,
            )
        )
    ).scalar_one()
    if existing:
        await write_audit(
            session,
            action="host.add_owner",
            target_kind="host",
            target_id=host_id,
            payload={"owner_kind": "team", "owner_id": str(team_id), "noop": True},
        )
        return False
    await session.execute(host_owner_teams.insert().values(host_id=host_id, team_id=team_id))
    await write_audit(
        session,
        action="host.add_owner",
        target_kind="host",
        target_id=host_id,
        payload={"owner_kind": "team", "owner_id": str(team_id)},
    )
    return True


async def remove_user_owner(
    session: AsyncSession,
    *,
    host_id: uuid.UUID,
    user_id: uuid.UUID,
) -> bool:
    host = await get_host(session, host_id)
    if host is None:
        raise NotFoundError("host not found")
    if host.status == "decommissioned":
        raise ConflictError("host is decommissioned; ownership is frozen")
    result = await session.execute(
        host_owner_users.delete().where(
            host_owner_users.c.host_id == host_id,
            host_owner_users.c.user_id == user_id,
        )
    )
    if result.rowcount:  # type: ignore[attr-defined]
        await write_audit(
            session,
            action="host.remove_owner",
            target_kind="host",
            target_id=host_id,
            payload={"owner_kind": "user", "owner_id": str(user_id)},
        )
        return True
    return False


async def remove_team_owner(
    session: AsyncSession,
    *,
    host_id: uuid.UUID,
    team_id: uuid.UUID,
) -> bool:
    host = await get_host(session, host_id)
    if host is None:
        raise NotFoundError("host not found")
    if host.status == "decommissioned":
        raise ConflictError("host is decommissioned; ownership is frozen")
    result = await session.execute(
        host_owner_teams.delete().where(
            host_owner_teams.c.host_id == host_id,
            host_owner_teams.c.team_id == team_id,
        )
    )
    if result.rowcount:  # type: ignore[attr-defined]
        await write_audit(
            session,
            action="host.remove_owner",
            target_kind="host",
            target_id=host_id,
            payload={"owner_kind": "team", "owner_id": str(team_id)},
        )
        return True
    return False
