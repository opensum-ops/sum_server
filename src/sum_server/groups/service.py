"""Group services: tree CRUD, memberships, parameters, resolution wrapper.

The ``global`` root group is protected: it cannot be renamed, reparented, or
deleted, and no second root can exist. All mutations are audited.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sum_server.core.audit import write_audit
from sum_server.core.errors import ConflictError, NotFoundError
from sum_server.core.ids import new_id
from sum_server.groups.models import (
    GLOBAL_GROUP_NAME,
    Group,
    GroupParameter,
    HostParameter,
    host_groups,
)
from sum_server.groups.resolution import (
    EffectiveParameter,
    GroupNode,
    ancestor_chain,
    resolve_parameters,
    subtree_ids,
)
from sum_server.history import service as history
from sum_server.hosts.models import Host

# --- Tree reads -------------------------------------------------------------


async def get_group(session: AsyncSession, group_id: uuid.UUID) -> Group | None:
    return (await session.execute(select(Group).where(Group.id == group_id))).scalar_one_or_none()


async def get_group_by_name(session: AsyncSession, name: str) -> Group | None:
    return (await session.execute(select(Group).where(Group.name == name))).scalar_one_or_none()


async def get_global_group(session: AsyncSession) -> Group:
    group = await get_group_by_name(session, GLOBAL_GROUP_NAME)
    if group is None:  # pragma: no cover - seeded by migration
        raise NotFoundError("global group missing; run migrations")
    return group


async def list_groups(session: AsyncSession) -> list[Group]:
    return list((await session.execute(select(Group).order_by(Group.name))).scalars().all())


async def distinct_parameter_keys(
    session: AsyncSession,
    *,
    actor_user_id: uuid.UUID,
    prefix: str = "",
    limit: int = 20,
) -> list[str]:
    """Parameter keys visible to this actor, for search suggestions.

    Group parameters are fleet-wide, so all group keys are offered; host-level
    keys are scoped to hosts the actor may read (same reasoning as
    :func:`~sum_server.hosts.facts.distinct_fact_keys`).
    """
    from sum_server.hosts.models import Host
    from sum_server.hosts.search import visibility_clause

    group_keys = select(GroupParameter.key)
    visible = select(Host.id).where(await visibility_clause(session, actor_user_id=actor_user_id))
    host_keys = select(HostParameter.key).where(HostParameter.host_id.in_(visible))
    if prefix:
        group_keys = group_keys.where(GroupParameter.key.ilike(f"{prefix}%"))
        host_keys = host_keys.where(HostParameter.key.ilike(f"{prefix}%"))

    # Two small capped queries and a merge, rather than a UNION whose ORDER BY
    # would have to reach into one branch's column.
    from_groups = (await session.execute(group_keys.distinct().limit(limit))).scalars().all()
    from_hosts = (await session.execute(host_keys.distinct().limit(limit))).scalars().all()
    return sorted(set(from_groups) | set(from_hosts))[:limit]


async def _nodes_by_id(session: AsyncSession) -> dict[uuid.UUID, GroupNode]:
    return {
        g.id: GroupNode(id=g.id, name=g.name, parent_id=g.parent_id)
        for g in await list_groups(session)
    }


# --- Tree writes ------------------------------------------------------------


async def create_group(
    session: AsyncSession,
    *,
    name: str,
    description: str | None,
    parent_id: uuid.UUID | None,
) -> Group:
    if name == GLOBAL_GROUP_NAME:
        raise ConflictError("the global group already exists")
    if await get_group_by_name(session, name) is not None:
        raise ConflictError("group name already in use")
    if parent_id is None:
        parent = await get_global_group(session)
    else:
        maybe_parent = await get_group(session, parent_id)
        if maybe_parent is None:
            raise NotFoundError("parent group not found")
        parent = maybe_parent
    group = Group(id=new_id(), name=name, description=description, parent_id=parent.id)
    session.add(group)
    await session.flush()
    await write_audit(
        session,
        action="group.create",
        target_kind="group",
        target_id=group.id,
        payload={"name": name, "parent": parent.name},
    )
    return group


async def _is_descendant(
    session: AsyncSession, *, ancestor_id: uuid.UUID, candidate_id: uuid.UUID
) -> bool:
    """True if ``candidate_id`` sits in ``ancestor_id``'s subtree."""
    nodes = await _nodes_by_id(session)
    return any(n.id == ancestor_id for n in ancestor_chain(candidate_id, nodes))


async def update_group(
    session: AsyncSession,
    *,
    group_id: uuid.UUID,
    name: str | None,
    description: str | None,
    parent_id: uuid.UUID | None,
) -> Group:
    group = await get_group(session, group_id)
    if group is None:
        raise NotFoundError("group not found")
    changed: dict[str, Any] = {}
    if group.name == GLOBAL_GROUP_NAME and (name is not None or parent_id is not None):
        raise ConflictError("the global group cannot be renamed or reparented")
    if name is not None and name != group.name:
        if name == GLOBAL_GROUP_NAME or await get_group_by_name(session, name) is not None:
            raise ConflictError("group name already in use")
        changed["name"] = {"old": group.name, "new": name}
        group.name = name
    if description is not None and description != group.description:
        group.description = description
        changed["description"] = True
    if parent_id is not None and parent_id != group.parent_id:
        if parent_id == group.id:
            raise ConflictError("a group cannot be its own parent")
        parent = await get_group(session, parent_id)
        if parent is None:
            raise NotFoundError("parent group not found")
        if await _is_descendant(session, ancestor_id=group.id, candidate_id=parent_id):
            raise ConflictError("cannot reparent a group under its own descendant")
        group.parent_id = parent_id
        changed["parent"] = parent.name
    if changed:
        await write_audit(
            session,
            action="group.update",
            target_kind="group",
            target_id=group.id,
            payload={"changed": changed},
        )
    return group


async def delete_group(session: AsyncSession, *, group_id: uuid.UUID) -> None:
    group = await get_group(session, group_id)
    if group is None:
        raise NotFoundError("group not found")
    if group.name == GLOBAL_GROUP_NAME:
        raise ConflictError("the global group cannot be deleted")
    children = (await session.execute(select(Group.id).where(Group.parent_id == group_id))).first()
    if children is not None:
        raise ConflictError("group has child groups; reparent them first")
    await write_audit(
        session,
        action="group.delete",
        target_kind="group",
        target_id=group.id,
        payload={"name": group.name},
    )
    await session.delete(group)


# --- Membership -------------------------------------------------------------


async def list_member_host_ids(session: AsyncSession, *, group_id: uuid.UUID) -> list[uuid.UUID]:
    rows = (
        await session.execute(
            select(host_groups.c.host_id).where(host_groups.c.group_id == group_id)
        )
    ).scalars()
    return list(rows)


async def list_effective_member_host_ids(
    session: AsyncSession, *, group_id: uuid.UUID
) -> list[uuid.UUID]:
    """Hosts in this group **or any group beneath it**.

    What a person means by "what is in this group". Direct membership is still
    what they edit; see :func:`list_member_host_ids` for that.
    """
    nodes = await _nodes_by_id(session)
    node = nodes.get(group_id)
    if node is None:
        raise NotFoundError("group not found")
    if node.parent_id is None:
        # The root. Membership in it is implicit and never written to
        # `host_groups`, so a subtree query would miss every host that is in no
        # group at all.
        return list((await session.execute(select(Host.id))).scalars().all())
    rows = (
        await session.execute(
            select(host_groups.c.host_id)
            .where(host_groups.c.group_id.in_(subtree_ids(group_id, nodes)))
            .distinct()
        )
    ).scalars()
    return list(rows)


async def effective_member_counts(session: AsyncSession) -> dict[uuid.UUID, int]:
    """Effective member count for every group, in a fixed number of queries.

    The membership pairs are counted in Python rather than per group in SQL: a
    host in two subgroups of the same parent must count once there, which a
    per-group ``COUNT`` cannot see without one query per group.
    """
    nodes = await _nodes_by_id(session)
    pairs = (
        (await session.execute(select(host_groups.c.group_id, host_groups.c.host_id)))
        .tuples()
        .all()
    )
    direct: dict[uuid.UUID, set[uuid.UUID]] = {}
    for group_id, host_id in pairs:
        direct.setdefault(group_id, set()).add(host_id)

    total_hosts = (await session.execute(select(func.count()).select_from(Host))).scalar_one()

    counts: dict[uuid.UUID, int] = {}
    for node in nodes.values():
        if node.parent_id is None:
            counts[node.id] = total_hosts
            continue
        members: set[uuid.UUID] = set()
        for gid in subtree_ids(node.id, nodes):
            members |= direct.get(gid, set())
        counts[node.id] = len(members)
    return counts


async def effective_membership_sources(
    session: AsyncSession, *, group_id: uuid.UUID
) -> dict[uuid.UUID, list[Group]]:
    """Each effective member, mapped to the groups in this subtree it is in.

    The group page needs to say *why* a host is listed: a direct member shows
    the group itself, an inherited one shows the subgroup it actually joined,
    and a host in two subgroups shows both. Under the root, a host in no group
    at all maps to an empty list, which is exactly its story.
    """
    nodes = await _nodes_by_id(session)
    node = nodes.get(group_id)
    if node is None:
        raise NotFoundError("group not found")
    wanted = subtree_ids(group_id, nodes)

    stmt = select(host_groups.c.host_id, Group).join(Group, Group.id == host_groups.c.group_id)
    if node.parent_id is not None:
        stmt = stmt.where(host_groups.c.group_id.in_(wanted))
    else:
        # Every host is an implicit member of the root, including hosts in no
        # group, so membership is seeded from the host table rather than the
        # join table.
        stmt = stmt.where(host_groups.c.group_id.in_(wanted - {group_id}))

    sources: dict[uuid.UUID, list[Group]] = {}
    if node.parent_id is None:
        for host_id in (await session.execute(select(Host.id))).scalars().all():
            sources[host_id] = []
    for host_id, group in (await session.execute(stmt.order_by(Group.name))).tuples().all():
        sources.setdefault(host_id, []).append(group)
    return sources


async def list_groups_for_host(session: AsyncSession, *, host_id: uuid.UUID) -> list[Group]:
    return list(
        (
            await session.execute(
                select(Group)
                .join(host_groups, host_groups.c.group_id == Group.id)
                .where(host_groups.c.host_id == host_id)
                .order_by(Group.name)
            )
        )
        .scalars()
        .all()
    )


async def add_member(session: AsyncSession, *, group_id: uuid.UUID, host_id: uuid.UUID) -> bool:
    from sum_server.hosts.service import get_host

    group = await get_group(session, group_id)
    if group is None:
        raise NotFoundError("group not found")
    if group.name == GLOBAL_GROUP_NAME:
        raise ConflictError("all hosts are implicit members of the global group")
    host = await get_host(session, host_id)
    if host is None:
        raise NotFoundError("host not found")
    existing = (
        await session.execute(
            select(host_groups.c.host_id).where(
                host_groups.c.group_id == group_id, host_groups.c.host_id == host_id
            )
        )
    ).first()
    if existing is not None:
        return False
    await session.execute(host_groups.insert().values(group_id=group_id, host_id=host_id))
    history.record(
        session,
        host_id=host_id,
        scope="group",
        field="membership",
        change="add",
        subject_id=group_id,
        subject_label=group.name,
    )
    await write_audit(
        session,
        action="group.add_member",
        target_kind="group",
        target_id=group_id,
        payload={"host_id": str(host_id)},
    )
    return True


async def remove_member(session: AsyncSession, *, group_id: uuid.UUID, host_id: uuid.UUID) -> bool:
    # Read the name before the delete: the timeline says which group was left,
    # and after this returns the caller has no membership row to look it up by.
    group = await get_group(session, group_id)
    result = await session.execute(
        host_groups.delete().where(
            host_groups.c.group_id == group_id, host_groups.c.host_id == host_id
        )
    )
    if result.rowcount:  # type: ignore[attr-defined]
        history.record(
            session,
            host_id=host_id,
            scope="group",
            field="membership",
            change="del",
            subject_id=group_id,
            subject_label=group.name if group is not None else None,
        )
        await write_audit(
            session,
            action="group.remove_member",
            target_kind="group",
            target_id=group_id,
            payload={"host_id": str(host_id)},
        )
        return True
    return False


# --- Parameters -------------------------------------------------------------


async def list_group_parameters(
    session: AsyncSession, *, group_id: uuid.UUID
) -> list[GroupParameter]:
    return list(
        (
            await session.execute(
                select(GroupParameter)
                .where(GroupParameter.group_id == group_id)
                .order_by(GroupParameter.key)
            )
        )
        .scalars()
        .all()
    )


async def set_group_parameter(
    session: AsyncSession, *, group_id: uuid.UUID, key: str, value: Any
) -> GroupParameter:
    group = await get_group(session, group_id)
    if group is None:
        raise NotFoundError("group not found")
    row = (
        await session.execute(
            select(GroupParameter).where(
                GroupParameter.group_id == group_id, GroupParameter.key == key
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = GroupParameter(id=new_id(), group_id=group_id, key=key, value=value)
        session.add(row)
    else:
        row.value = value
    await write_audit(
        session,
        action="group.set_parameter",
        target_kind="group",
        target_id=group_id,
        payload={"key": key},
    )
    return row


async def unset_group_parameter(session: AsyncSession, *, group_id: uuid.UUID, key: str) -> bool:
    row = (
        await session.execute(
            select(GroupParameter).where(
                GroupParameter.group_id == group_id, GroupParameter.key == key
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    await session.delete(row)
    await write_audit(
        session,
        action="group.unset_parameter",
        target_kind="group",
        target_id=group_id,
        payload={"key": key},
    )
    return True


async def list_host_parameters(session: AsyncSession, *, host_id: uuid.UUID) -> list[HostParameter]:
    return list(
        (
            await session.execute(
                select(HostParameter)
                .where(HostParameter.host_id == host_id)
                .order_by(HostParameter.key)
            )
        )
        .scalars()
        .all()
    )


async def set_host_parameter(
    session: AsyncSession, *, host_id: uuid.UUID, key: str, value: Any
) -> HostParameter:
    row = (
        await session.execute(
            select(HostParameter).where(HostParameter.host_id == host_id, HostParameter.key == key)
        )
    ).scalar_one_or_none()
    if row is None:
        history.record(session, host_id=host_id, scope="param", field=key, change="add", new=value)
        row = HostParameter(id=new_id(), host_id=host_id, key=key, value=value)
        session.add(row)
    else:
        # Re-setting a parameter to what it already was is not a change.
        if row.value != value:
            history.record(
                session,
                host_id=host_id,
                scope="param",
                field=key,
                change="edit",
                old=row.value,
                new=value,
            )
        row.value = value
    await write_audit(
        session,
        action="host.set_parameter",
        target_kind="host",
        target_id=host_id,
        payload={"key": key},
    )
    return row


async def unset_host_parameter(session: AsyncSession, *, host_id: uuid.UUID, key: str) -> bool:
    row = (
        await session.execute(
            select(HostParameter).where(HostParameter.host_id == host_id, HostParameter.key == key)
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    history.record(session, host_id=host_id, scope="param", field=key, change="del", old=row.value)
    await session.delete(row)
    await write_audit(
        session,
        action="host.unset_parameter",
        target_kind="host",
        target_id=host_id,
        payload={"key": key},
    )
    return True


# --- Resolution -------------------------------------------------------------


async def effective_parameters_for_host(
    session: AsyncSession, *, host_id: uuid.UUID
) -> list[EffectiveParameter]:
    """Load the tree + parameters and resolve for one host (sorted by key)."""
    nodes = await _nodes_by_id(session)
    global_id = next(n.id for n in nodes.values() if n.name == GLOBAL_GROUP_NAME)
    member_ids = list(
        (
            await session.execute(
                select(host_groups.c.group_id).where(host_groups.c.host_id == host_id)
            )
        ).scalars()
    )
    group_params: dict[uuid.UUID, dict[str, Any]] = {}
    for gp in (await session.execute(select(GroupParameter))).scalars():
        group_params.setdefault(gp.group_id, {})[gp.key] = gp.value
    host_params = {hp.key: hp.value for hp in await list_host_parameters(session, host_id=host_id)}
    resolved = resolve_parameters(
        groups_by_id=nodes,
        group_params=group_params,
        host_params=host_params,
        member_group_ids=member_ids,
        global_group_id=global_id,
    )
    return [resolved[k] for k in sorted(resolved)]
