"""Server services: CRUD, ownership (M:N users + teams), visibility scoping."""

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
from sum_server.servers.models import (
    Server,
    server_owner_teams,
    server_owner_users,
)
from sum_server.servers.schemas import ServerCreate, ServerUpdate


async def get_server(session: AsyncSession, server_id: uuid.UUID) -> Server | None:
    return (
        await session.execute(select(Server).where(Server.id == server_id))
    ).scalar_one_or_none()


async def get_user_owner_ids(session: AsyncSession, server_id: uuid.UUID) -> list[uuid.UUID]:
    rows = (
        (
            await session.execute(
                select(server_owner_users.c.user_id).where(
                    server_owner_users.c.server_id == server_id
                )
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def get_team_owner_ids(session: AsyncSession, server_id: uuid.UUID) -> list[uuid.UUID]:
    rows = (
        (
            await session.execute(
                select(server_owner_teams.c.team_id).where(
                    server_owner_teams.c.server_id == server_id
                )
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def user_can_read(session: AsyncSession, server: Server, user_id: uuid.UUID) -> bool:
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
            .select_from(server_owner_users)
            .where(
                server_owner_users.c.server_id == server.id,
                server_owner_users.c.user_id == user_id,
            )
        )
    ).scalar_one()
    if direct:
        return True

    from sum_server.teams.models import TeamMembership

    via_team = (
        await session.execute(
            select(func.count())
            .select_from(server_owner_teams)
            .join(
                TeamMembership,
                and_(
                    TeamMembership.team_id == server_owner_teams.c.team_id,
                    TeamMembership.user_id == user_id,
                ),
            )
            .where(server_owner_teams.c.server_id == server.id)
        )
    ).scalar_one()
    return bool(via_team)


async def list_servers_visible_to(
    session: AsyncSession,
    *,
    actor_user_id: uuid.UUID,
    limit: int,
    cursor: Cursor | None,
    status_filter: str | None = None,
) -> list[Server]:
    from sum_server.teams.models import TeamMembership
    from sum_server.users.service import get_user

    user = await get_user(session, actor_user_id)
    if user is None:
        return []
    stmt = select(Server)
    if not user.is_admin:
        stmt = stmt.where(
            or_(
                Server.id.in_(
                    select(server_owner_users.c.server_id).where(
                        server_owner_users.c.user_id == actor_user_id
                    )
                ),
                Server.id.in_(
                    select(server_owner_teams.c.server_id)
                    .join(
                        TeamMembership,
                        TeamMembership.team_id == server_owner_teams.c.team_id,
                    )
                    .where(TeamMembership.user_id == actor_user_id)
                ),
            )
        )
    if status_filter is not None:
        stmt = stmt.where(Server.status == status_filter)
    if cursor is not None:
        stmt = stmt.where(
            (Server.created_at, Server.id) < (cursor.ts, cursor.id)  # type: ignore[operator]
        )
    stmt = stmt.order_by(Server.created_at.desc(), Server.id.desc()).limit(limit + 1)
    return list((await session.execute(stmt)).scalars().all())


async def create_server(session: AsyncSession, payload: ServerCreate) -> Server:
    if payload.status not in ("provisioning", "active"):
        raise ConflictError("new servers must be provisioning or active")
    server = Server(
        id=new_id(),
        name=payload.name.strip(),
        hostname=payload.hostname,
        description=payload.description,
        status=payload.status,
    )
    session.add(server)
    await session.flush()
    await write_audit(
        session,
        action="server.create",
        target_kind="server",
        target_id=server.id,
        payload={"name": server.name, "status": server.status},
    )
    return server


async def update_server(
    session: AsyncSession,
    *,
    server_id: uuid.UUID,
    payload: ServerUpdate,
    if_match_version: int | None = None,
) -> Server:
    server = await get_server(session, server_id)
    if server is None:
        raise NotFoundError("server not found")
    if if_match_version is not None and server.version != if_match_version:
        raise PreconditionFailedError("server has been modified")
    if server.status == "decommissioned":
        raise ConflictError("server is decommissioned; cannot modify")
    changed: dict[str, object] = {}
    if payload.name is not None and payload.name.strip() != server.name:
        server.name = payload.name.strip()
        changed["name"] = server.name
    if payload.hostname is not None and payload.hostname != server.hostname:
        server.hostname = payload.hostname
        changed["hostname"] = server.hostname
    if payload.description is not None and payload.description != server.description:
        server.description = payload.description
        changed["description"] = server.description
    if payload.status is not None and payload.status != server.status:
        if payload.status == "decommissioned":
            raise ConflictError("use DELETE to decommission a server")
        server.status = payload.status
        changed["status"] = payload.status
    if changed:
        await write_audit(
            session,
            action="server.update",
            target_kind="server",
            target_id=server.id,
            payload={"changed": list(changed.keys())},
        )
    return server


async def decommission_server(session: AsyncSession, *, server_id: uuid.UUID) -> Server:
    server = await get_server(session, server_id)
    if server is None:
        raise NotFoundError("server not found")
    if server.status == "decommissioned":
        return server
    server.status = "decommissioned"
    await write_audit(
        session,
        action="server.decommission",
        target_kind="server",
        target_id=server_id,
        payload={},
    )
    return server


async def add_user_owner(
    session: AsyncSession,
    *,
    server_id: uuid.UUID,
    user_id: uuid.UUID,
) -> bool:
    server = await get_server(session, server_id)
    if server is None:
        raise NotFoundError("server not found")
    if server.status == "decommissioned":
        raise ConflictError("server is decommissioned; ownership is frozen")
    from sum_server.users.service import get_user

    user = await get_user(session, user_id)
    if user is None or user.deleted_at is not None:
        raise NotFoundError("user not found")
    existing = (
        await session.execute(
            select(func.count())
            .select_from(server_owner_users)
            .where(
                server_owner_users.c.server_id == server_id,
                server_owner_users.c.user_id == user_id,
            )
        )
    ).scalar_one()
    if existing:
        await write_audit(
            session,
            action="server.add_owner",
            target_kind="server",
            target_id=server_id,
            payload={"owner_kind": "user", "owner_id": str(user_id), "noop": True},
        )
        return False
    await session.execute(server_owner_users.insert().values(server_id=server_id, user_id=user_id))
    await write_audit(
        session,
        action="server.add_owner",
        target_kind="server",
        target_id=server_id,
        payload={"owner_kind": "user", "owner_id": str(user_id)},
    )
    return True


async def add_team_owner(
    session: AsyncSession,
    *,
    server_id: uuid.UUID,
    team_id: uuid.UUID,
) -> bool:
    server = await get_server(session, server_id)
    if server is None:
        raise NotFoundError("server not found")
    if server.status == "decommissioned":
        raise ConflictError("server is decommissioned; ownership is frozen")
    from sum_server.teams.service import get_team

    team = await get_team(session, team_id)
    if team is None:
        raise NotFoundError("team not found")
    existing = (
        await session.execute(
            select(func.count())
            .select_from(server_owner_teams)
            .where(
                server_owner_teams.c.server_id == server_id,
                server_owner_teams.c.team_id == team_id,
            )
        )
    ).scalar_one()
    if existing:
        await write_audit(
            session,
            action="server.add_owner",
            target_kind="server",
            target_id=server_id,
            payload={"owner_kind": "team", "owner_id": str(team_id), "noop": True},
        )
        return False
    await session.execute(server_owner_teams.insert().values(server_id=server_id, team_id=team_id))
    await write_audit(
        session,
        action="server.add_owner",
        target_kind="server",
        target_id=server_id,
        payload={"owner_kind": "team", "owner_id": str(team_id)},
    )
    return True


async def remove_user_owner(
    session: AsyncSession,
    *,
    server_id: uuid.UUID,
    user_id: uuid.UUID,
) -> bool:
    server = await get_server(session, server_id)
    if server is None:
        raise NotFoundError("server not found")
    if server.status == "decommissioned":
        raise ConflictError("server is decommissioned; ownership is frozen")
    result = await session.execute(
        server_owner_users.delete().where(
            server_owner_users.c.server_id == server_id,
            server_owner_users.c.user_id == user_id,
        )
    )
    if result.rowcount:  # type: ignore[attr-defined]
        await write_audit(
            session,
            action="server.remove_owner",
            target_kind="server",
            target_id=server_id,
            payload={"owner_kind": "user", "owner_id": str(user_id)},
        )
        return True
    return False


async def remove_team_owner(
    session: AsyncSession,
    *,
    server_id: uuid.UUID,
    team_id: uuid.UUID,
) -> bool:
    server = await get_server(session, server_id)
    if server is None:
        raise NotFoundError("server not found")
    if server.status == "decommissioned":
        raise ConflictError("server is decommissioned; ownership is frozen")
    result = await session.execute(
        server_owner_teams.delete().where(
            server_owner_teams.c.server_id == server_id,
            server_owner_teams.c.team_id == team_id,
        )
    )
    if result.rowcount:  # type: ignore[attr-defined]
        await write_audit(
            session,
            action="server.remove_owner",
            target_kind="server",
            target_id=server_id,
            payload={"owner_kind": "team", "owner_id": str(team_id)},
        )
        return True
    return False
