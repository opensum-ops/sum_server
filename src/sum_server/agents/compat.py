"""Agent version negotiation for best-effort backwards compatibility.

The server supports agents at N-1 minor (see [[Roadmap]]). Agent-facing schema
changes must stay **additive** (new request fields optional with defaults, new
response fields ignored by older agents). This module reads the agent's version
from the request and answers whether it is within the support window.
"""

from __future__ import annotations

from fastapi import Request

from sum_server import __version__
from sum_server.core.versions import parse_version, within_one_minor


def agent_version_from_request(request: Request, explicit: str | None = None) -> str | None:
    """Determine the agent version: an explicit request field wins, else the
    ``User-Agent`` header (``sum-agent/X.Y.Z``).
    """
    if explicit and parse_version(explicit):
        return explicit
    ua = request.headers.get("user-agent", "")
    parsed = parse_version(ua) if "sum-agent" in ua.lower() else None
    return ".".join(str(p) for p in parsed) if parsed else None


def is_supported(agent_version: str | None) -> bool:
    """True if the agent is within the N-1 support window of this server.

    Unknown versions are treated as supported (best-effort — never reject on a
    parse failure), consistent with the additive-only guarantee.
    """
    if agent_version is None:
        return True
    return within_one_minor(agent_version, __version__)
