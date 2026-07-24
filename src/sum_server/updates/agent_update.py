"""Per-host agent update: set/clear the target version and build the signed
heartbeat directive.

The slow work (downloading + caching the binary) happens when the admin
requests the update, not on the heartbeat path — so heartbeats stay fast and
only consult the on-disk cache.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from sum_server.agents.schemas import AgentUpdateDirective
from sum_server.core.audit import write_audit
from sum_server.core.context import Actor
from sum_server.hosts.models import Host
from sum_server.updates import agent_binary
from sum_server.updates.directive import build_directive

# Arches we can serve a binary for (the host ``arch`` fact).
_SUPPORTED_ARCH = {"x86_64", "amd64"}


async def set_target(session: AsyncSession, *, host: Host, version: str, actor: Actor) -> None:
    """Set the host's desired agent version (binary must already be cached)."""
    host.target_agent_version = version
    await write_audit(
        session,
        action="host.agent_update_requested",
        target_kind="host",
        target_id=host.id,
        payload={"target_version": version},
        actor_kind=actor.kind,
        actor_id=actor.id,
    )


async def clear_target(session: AsyncSession, *, host: Host, reason: str) -> None:
    prev = host.target_agent_version
    host.target_agent_version = None
    await write_audit(
        session,
        action="host.agent_update_cleared",
        target_kind="host",
        target_id=host.id,
        payload={"was": prev, "reason": reason},
    )


async def build_directive_for_host(
    session: AsyncSession,
    *,
    host: Host,
    reported_version: str | None,
    host_arch: str | None,
    base_url: str,
) -> AgentUpdateDirective | None:
    """Return a signed update directive for this heartbeat, or ``None``.

    Side effect: if the agent already reached the target, clears the target
    (update confirmed) and returns ``None``.
    """
    target = host.target_agent_version
    if not target:
        return None
    if reported_version == target:
        await clear_target(session, host=host, reason="reached_target")
        return None
    if host_arch is not None and host_arch not in _SUPPORTED_ARCH:
        return None
    cached = agent_binary.cached_binary_if_present(target)
    if cached is None:
        # Not yet cached (should have been done at request time); skip quietly.
        return None
    directive = build_directive(
        host_id=host.id,
        target_version=target,
        sha256=cached.sha256,
        binary_url=f"{base_url.rstrip('/')}/api/v1/agents/binary/{target}",
    )
    return AgentUpdateDirective(**directive)
