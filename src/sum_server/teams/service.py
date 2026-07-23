"""Team services: CRUD with last-admin guard, host-ownership guard."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sum_server.core.audit import write_audit
from sum_server.core.errors import ConflictError, NotFoundError, PreconditionFailedError
from sum_server.core.ids import new_id
from sum_server.core.pagination import Cursor
from sum_server.teams.models import Team, TeamMembership
from sum_server.teams.schemas import TeamCreate, TeamUpdate


def _normalize_name(name: str) -> str:
    return name.strip()


async def get_team(session: AsyncSession, team_id: uuid.UUID) -> Team | None:
    return (await session.execute(select(Team).where(Team.id == team_id))).scalar_one_or_none()


async def get_team_by_name(session: AsyncSession, name: str) -> Team | None:
    return (
        await session.execute(select(Team).where(func.lower(Team.name) == name.lower()))
    ).scalar_one_or_none()


async def list_teams(session: AsyncSession, *, limit: int, cursor: Cursor | None) -> list[Team]:
    stmt = select(Team)
    if cursor is not None:
        stmt = stmt.where(
            (Team.created_at, Team.id) < (cursor.ts, cursor.id)  # type: ignore[operator]
        )
    stmt = stmt.order_by(Team.created_at.desc(), Team.id.desc()).limit(limit + 1)
    return list((await session.execute(stmt)).scalars().all())


async def create_team(session: AsyncSession, payload: TeamCreate) -> Team:
    name = _normalize_name(payload.name)
    if await get_team_by_name(session, name) is not None:
        raise ConflictError("a team with that name already exists")
    team = Team(id=new_id(), name=name, description=payload.description)
    session.add(team)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError("a team with that name already exists") from exc
    await write_audit(
        session,
        action="team.create",
        target_kind="team",
        target_id=team.id,
        payload={"name": team.name},
    )
    return team


async def update_team(
    session: AsyncSession,
    *,
    team_id: uuid.UUID,
    payload: TeamUpdate,
    if_match_version: int | None = None,
) -> Team:
    team = await get_team(session, team_id)
    if team is None:
        raise NotFoundError("team not found")
    if if_match_version is not None and team.version != if_match_version:
        raise PreconditionFailedError("team has been modified")
    changed: dict[str, object] = {}
    if payload.name is not None:
        new_name = _normalize_name(payload.name)
        if new_name.lower() != team.name.lower():
            clash = await get_team_by_name(session, new_name)
            if clash is not None and clash.id != team.id:
                raise ConflictError("another team already uses that name")
        if new_name != team.name:
            team.name = new_name
            changed["name"] = new_name
    if payload.description is not None and payload.description != team.description:
        team.description = payload.description
        changed["description"] = payload.description
    if changed:
        await write_audit(
            session,
            action="team.update",
            target_kind="team",
            target_id=team.id,
            payload={"changed": list(changed.keys())},
        )
    return team


async def delete_team(session: AsyncSession, *, team_id: uuid.UUID) -> Team:
    team = await get_team(session, team_id)
    if team is None:
        raise NotFoundError("team not found")
    # Refuse deletion if team owns active hosts. Local import to avoid cycles.
    from sum_server.hosts.models import Host, host_owner_teams

    affected = (
        (
            await session.execute(
                select(Host.id)
                .join(host_owner_teams, Host.id == host_owner_teams.c.host_id)
                .where(
                    host_owner_teams.c.team_id == team_id,
                    Host.status != "decommissioned",
                )
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    if affected:
        raise ConflictError(
            "team owns active hosts; reassign before deletion",
            details={"server_ids": [str(s) for s in affected]},
        )
    await session.delete(team)
    await write_audit(
        session,
        action="team.delete",
        target_kind="team",
        target_id=team.id,
        payload={"name": team.name},
    )
    return team


async def add_member(
    session: AsyncSession,
    *,
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
) -> TeamMembership:
    if await get_team(session, team_id) is None:
        raise NotFoundError("team not found")
    from sum_server.users.service import get_user

    user = await get_user(session, user_id)
    if user is None or user.deleted_at is not None:
        raise NotFoundError("user not found")

    existing = (
        await session.execute(
            select(TeamMembership).where(
                TeamMembership.team_id == team_id, TeamMembership.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    m = TeamMembership(id=new_id(), team_id=team_id, user_id=user_id, role=role)
    session.add(m)
    await write_audit(
        session,
        action="team.add_member",
        target_kind="team",
        target_id=team_id,
        payload={"user_id": str(user_id), "role": role},
    )
    return m


async def _count_team_admins(session: AsyncSession, team_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(TeamMembership)
                .where(TeamMembership.team_id == team_id, TeamMembership.role == "admin")
            )
        ).scalar_one()
    )


async def update_member_role(
    session: AsyncSession,
    *,
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
) -> TeamMembership:
    m = (
        await session.execute(
            select(TeamMembership).where(
                TeamMembership.team_id == team_id, TeamMembership.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if m is None:
        raise NotFoundError("membership not found")
    if m.role == "admin" and role != "admin":
        admins = await _count_team_admins(session, team_id)
        if admins <= 1:
            raise ConflictError("cannot demote the last team admin")
    if m.role == role:
        return m
    m.role = role
    await write_audit(
        session,
        action="team.update_member",
        target_kind="team",
        target_id=team_id,
        payload={"user_id": str(user_id), "role": role},
    )
    return m


async def remove_member(session: AsyncSession, *, team_id: uuid.UUID, user_id: uuid.UUID) -> None:
    m = (
        await session.execute(
            select(TeamMembership).where(
                TeamMembership.team_id == team_id, TeamMembership.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if m is None:
        raise NotFoundError("membership not found")
    if m.role == "admin":
        admins = await _count_team_admins(session, team_id)
        if admins <= 1:
            raise ConflictError("cannot remove the last team admin")
    await session.delete(m)
    await write_audit(
        session,
        action="team.remove_member",
        target_kind="team",
        target_id=team_id,
        payload={"user_id": str(user_id)},
    )


async def is_team_admin(session: AsyncSession, *, team_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    role = (
        await session.execute(
            select(TeamMembership.role).where(
                TeamMembership.team_id == team_id, TeamMembership.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    return role == "admin"
