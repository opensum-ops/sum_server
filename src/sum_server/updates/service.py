"""Update services: refresh the release cache from GitHub and read it back."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sum_server import __version__
from sum_server.core.audit import write_audit
from sum_server.core.versions import is_newer, parse_version
from sum_server.updates.github import ReleaseFetchError, fetch_latest_release
from sum_server.updates.models import COMPONENT_AGENT, COMPONENT_SERVER, ReleaseCache
from sum_server.updates.schemas import ComponentUpdateStatus, UpdatesSummary


def _utcnow() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


async def get_release_cache(session: AsyncSession, repo: str) -> ReleaseCache | None:
    return (
        await session.execute(select(ReleaseCache).where(ReleaseCache.repo == repo))
    ).scalar_one_or_none()


async def _upsert(session: AsyncSession, repo: str) -> ReleaseCache:
    row = await get_release_cache(session, repo)
    if row is None:
        row = ReleaseCache(repo=repo)
        session.add(row)
    now = _utcnow()
    try:
        info = await fetch_latest_release(repo)
    except ReleaseFetchError as exc:
        row.error = str(exc)
        row.checked_at = now
        return row
    row.latest_version = info.version
    row.name = info.name
    row.notes = info.notes[:16000]
    row.published_at = info.published_at
    row.assets = info.assets
    row.checked_at = now
    row.error = None
    return row


async def refresh_all(session: AsyncSession, *, audit: bool = True) -> dict[str, ReleaseCache]:
    """Refresh both components' release caches. Records one audit entry."""
    out = {
        COMPONENT_SERVER: await _upsert(session, COMPONENT_SERVER),
        COMPONENT_AGENT: await _upsert(session, COMPONENT_AGENT),
    }
    if audit:
        await write_audit(
            session,
            action="update.check",
            target_kind="system",
            target_id=None,
            payload={repo: (r.latest_version or r.error) for repo, r in out.items()},
        )
    return out


async def fleet_agent_version(session: AsyncSession) -> str | None:
    """The newest agent version running across the fleet (max ``agent_version``
    fact), or ``None`` if no agent has reported one yet.
    """
    from sum_server.hosts.models import HostFact

    rows = (
        (
            await session.execute(
                select(func.distinct(HostFact.value)).where(HostFact.key == "agent_version")
            )
        )
        .scalars()
        .all()
    )
    parsed = [(parse_version(str(v)), str(v)) for v in rows if v]
    valid = [(p, s) for p, s in parsed if p is not None]
    if not valid:
        return None
    return max(valid, key=lambda ps: ps[0])[1]


def _status(component: str, current: str, cache: ReleaseCache | None) -> ComponentUpdateStatus:
    latest = cache.latest_version if cache else None
    return ComponentUpdateStatus(
        component=component,
        current_version=current,
        latest_version=latest,
        update_available=is_newer(latest, current) if (latest and current) else bool(latest),
        release_name=cache.name if cache else None,
        notes=cache.notes if cache else None,
        published_at=cache.published_at if cache else None,
        checked_at=cache.checked_at if cache else None,
        error=cache.error if cache else None,
    )


async def build_summary(session: AsyncSession) -> UpdatesSummary:
    server_cache = await get_release_cache(session, COMPONENT_SERVER)
    agent_cache = await get_release_cache(session, COMPONENT_AGENT)
    agent_current = await fleet_agent_version(session) or ""
    return UpdatesSummary(
        server=_status(COMPONENT_SERVER, __version__, server_cache),
        agent=_status(COMPONENT_AGENT, agent_current, agent_cache),
    )
