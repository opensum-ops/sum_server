"""Deleting host records whose enrollment never completed.

The wizard creates a host row before anything is installed anywhere, so a token
generated and never used leaves a permanent ``pending`` row on the hosts page.
Nothing was ever put on a machine for these, so there is nothing to uninstall
and no agent to tell: this is the automatic counterpart of
:func:`sum_server.hosts.removal.delete_host_record`, and it goes through that
same function so the two paths cannot drift.

The rule is deliberately conservative, because this is the first thing in the
project that destroys data with no human in the loop:

* the host must **never have been enrolled** (no ``agent_tokens`` row has ever
  existed, revoked or not). Presence is not consulted, for the reason spelled
  out in :func:`sum_server.hosts.removal.was_ever_enrolled`: it returns to
  ``pending`` after an agent removal, so a presence-keyed rule would delete real
  machines.
* it must never have heartbeated, which is a second, independent way of asking
  the same question. A host that has heartbeated has been alive.
* it must have **at least one enrollment**, all of them unused, and the latest
  expiry must be older than the grace period. A host with no enrollment at all
  has no enrollment period to have expired, so it is left alone rather than
  guessed about.

The grace period is separate from, and much longer than, the token's own TTL:
token expiry means "this token no longer works", which is not on its own a
reason to delete the operator's record of a machine they are still setting up.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sum_server.agents.models import AgentEnrollment
from sum_server.auth.models import AgentToken
from sum_server.core.context import SYSTEM_ACTOR
from sum_server.hosts import removal
from sum_server.hosts.models import Host

log = structlog.get_logger(__name__)

DELETE_REASON = "enrollment_expired"


@dataclass(frozen=True, slots=True)
class Swept:
    """One deleted host, kept for logging and for the caller's report."""

    host_id: uuid.UUID
    hostname: str
    expired_at: dt.datetime


def _utcnow() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


async def find_stale(
    session: AsyncSession, *, grace_seconds: int, now: dt.datetime | None = None
) -> list[tuple[Host, dt.datetime]]:
    """Hosts eligible for deletion, each with the expiry that made it eligible.

    Returned rather than deleted so the rule can be tested, and read, on its
    own.
    """
    at = now or _utcnow()
    cutoff = at - dt.timedelta(seconds=grace_seconds)

    ever_enrolled = select(AgentToken.id).where(AgentToken.host_id == Host.id)
    used_enrollment = select(AgentEnrollment.id).where(
        AgentEnrollment.host_id == Host.id, AgentEnrollment.used_at.is_not(None)
    )
    # The last moment any token for this host was usable. Grouping in a
    # correlated subquery keeps the whole rule in one statement, so a host is
    # never judged against a half-loaded set of its enrollments.
    latest_expiry = (
        select(func.max(AgentEnrollment.expires_at))
        .where(AgentEnrollment.host_id == Host.id)
        .correlate(Host)
        .scalar_subquery()
    )

    stmt = (
        select(Host, latest_expiry.label("expired_at"))
        .where(
            Host.last_heartbeat_at.is_(None),
            ~ever_enrolled.exists(),
            ~used_enrollment.exists(),
            latest_expiry.is_not(None),
            latest_expiry < cutoff,
        )
        .order_by(Host.created_at)
    )
    rows = (await session.execute(stmt)).all()
    return [(host, expired_at) for host, expired_at in rows]


async def sweep(
    session: AsyncSession, *, grace_seconds: int, now: dt.datetime | None = None
) -> list[Swept]:
    """Delete every eligible host, one audit entry each.

    Attribution is the system actor: no user pressed anything, and an audit
    entry that named one would be a lie about who decided. The entries survive
    the delete, so the trail of what was removed outlives the rows.
    """
    swept: list[Swept] = []
    for host, expired_at in await find_stale(session, grace_seconds=grace_seconds, now=now):
        await removal.delete_host_record(
            session, host=host, actor=SYSTEM_ACTOR, reason=DELETE_REASON
        )
        swept.append(Swept(host_id=host.id, hostname=host.hostname, expired_at=expired_at))
    if swept:
        log.info(
            "stale_hosts_deleted",
            count=len(swept),
            hostnames=[s.hostname for s in swept],
        )
    return swept
