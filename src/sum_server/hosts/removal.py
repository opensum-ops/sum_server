"""Taking the agent off a host, and deleting a host that never had one.

Deliberately not decommissioning: retiring a real machine is a separate
lifecycle concern (``status = decommissioned``, reached by
``DELETE /api/v1/hosts/{id}``). This module only removes the agent, and
hard-deletes host records that never became managed machines.

The server cannot reach into a host to uninstall anything, per hard constraint
#1, so removal from a live host is desired state: ``request`` records the
intent, the agent collects a signed directive on its next heartbeat, and
``complete`` runs when it reports back. See [[Agent Removal]] in the vault.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sum_server.agents.models import AgentEnrollment
from sum_server.auth.models import AgentToken
from sum_server.components.models import Component
from sum_server.core.audit import write_audit
from sum_server.core.context import Actor
from sum_server.core.errors import ConflictError
from sum_server.history import service as history
from sum_server.history.models import HostChange
from sum_server.hosts.models import Host, HostFact


def _utcnow() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


async def was_ever_enrolled(session: AsyncSession, *, host_id: uuid.UUID) -> bool:
    """Whether an agent token has ever existed for this host, revoked or not.

    This, and not presence, decides whether a host can be deleted outright.
    Presence returns to ``pending`` after a successful removal, so keying off it
    would mean pressing "remove agent" a second time silently hard-deletes a
    real machine. Removal revokes tokens rather than deleting them precisely so
    this signal survives.
    """
    row = (
        await session.execute(select(AgentToken.id).where(AgentToken.host_id == host_id).limit(1))
    ).first()
    return row is not None


async def request(session: AsyncSession, *, host: Host, actor: Actor) -> None:
    """Ask the agent to remove itself on its next heartbeat."""
    if host.agent_removal_requested_at is not None:
        raise ConflictError("agent removal already requested")
    host.agent_removal_requested_at = _utcnow()
    # An update the agent is about to become unable to apply is noise; drop it
    # so the heartbeat path never has to decide between two directives.
    if host.target_agent_version is not None:
        host.target_agent_version = None
    await write_audit(
        session,
        action="host.agent_removal_requested",
        target_kind="host",
        target_id=host.id,
        payload={"hostname": host.hostname},
        actor_kind=actor.kind,
        actor_id=actor.id,
    )


async def cancel(session: AsyncSession, *, host: Host, reason: str) -> None:
    """Withdraw a pending removal. The agent simply stops being told to go."""
    if host.agent_removal_requested_at is None:
        raise ConflictError("no agent removal is pending")
    host.agent_removal_requested_at = None
    await write_audit(
        session,
        action="host.agent_removal_cancelled",
        target_kind="host",
        target_id=host.id,
        payload={"reason": reason},
    )


async def complete(session: AsyncSession, *, host: Host) -> dict[str, int]:
    """Clear everything agent-derived, once the agent reports it has gone.

    Credentials are **revoked**, not deleted: the revoked row is what remembers
    that this host was once a real managed machine (see
    :func:`was_ever_enrolled`). Agent-observed data is deleted outright.

    The audit log is never touched. ``audit_entries.target_id`` is deliberately
    not a foreign key, so entries outlive even a hard host delete; hard
    constraint #5 would mean little if a button could erase them.
    """
    now = _utcnow()
    counts: dict[str, int] = {}

    facts = await session.execute(delete(HostFact).where(HostFact.host_id == host.id))
    counts["facts"] = facts.rowcount or 0  # type: ignore[attr-defined]
    comps = await session.execute(delete(Component).where(Component.host_id == host.id))
    counts["components"] = comps.rowcount or 0  # type: ignore[attr-defined]

    # `actor_kind` is exactly the question being asked, and it gets the mixed
    # cases right without a special rule: hostname adoption and boot_id are
    # agent-written `host`-scope rows and go, while a human's description edit
    # on the same host stays.
    changes = await session.execute(
        delete(HostChange).where(HostChange.host_id == host.id, HostChange.actor_kind == "agent")
    )
    counts["changes"] = changes.rowcount or 0  # type: ignore[attr-defined]

    tokens = await session.execute(
        update(AgentToken)
        .where(AgentToken.host_id == host.id, AgentToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    counts["tokens_revoked"] = tokens.rowcount or 0  # type: ignore[attr-defined]
    enrollments = await session.execute(
        update(AgentEnrollment)
        .where(
            AgentEnrollment.host_id == host.id,
            AgentEnrollment.used_at.is_(None),
            AgentEnrollment.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    counts["enrollments_revoked"] = enrollments.rowcount or 0  # type: ignore[attr-defined]

    # Presence derives back to `pending` on its own once there is no heartbeat,
    # so there is no stored presence to reset.
    host.last_heartbeat_at = None
    host.reported_presence = None
    host.boot_id = None
    host.target_agent_version = None
    host.agent_removal_requested_at = None

    # Written *after* the sweep, so it is the one agent-written row that
    # survives it. That is deliberate: the host page has just lost every fact
    # and component, and this is the row on the Overview feed that explains
    # why. It is a tombstone, not inventory, which is the line the sweep is
    # actually drawing. A later removal clears this one too; the audit log
    # keeps every removal permanently, so nothing is lost by that.
    history.record(
        session,
        host_id=host.id,
        scope="host",
        field="agent",
        change="del",
        old="installed",
        at=now,
    )
    await write_audit(
        session,
        action="host.agent_removed",
        target_kind="host",
        target_id=host.id,
        payload={"hostname": host.hostname, **counts},
    )
    return counts


async def delete_host_record(session: AsyncSession, *, host: Host, actor: Actor) -> None:
    """Hard-delete a host that never had an agent.

    Only reachable for a host that was never enrolled, where the record is a
    placeholder the wizard created and nothing was ever installed anywhere.
    Every table referencing ``hosts.id`` cascades, so this takes facts,
    components, memberships, parameters, ownership, and change history with it.
    The audit trail survives, being keyed by a plain uuid rather than a foreign
    key.
    """
    if await was_ever_enrolled(session, host_id=host.id):
        raise ConflictError("host has been enrolled; remove the agent instead")
    # Audit before the delete: writing it after would order the entry against a
    # row that no longer exists, and the payload is the only remaining record.
    await write_audit(
        session,
        action="host.deleted",
        target_kind="host",
        target_id=host.id,
        payload={"hostname": host.hostname, "reason": "never_enrolled"},
        actor_kind=actor.kind,
        actor_id=actor.id,
    )
    await session.delete(host)
