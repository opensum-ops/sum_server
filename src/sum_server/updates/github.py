"""GitHub Releases client. The server is the only component that talks to GitHub.

Anonymous by default (60 req/hr is ample for periodic checks); an optional
token raises the limit. Network/offline/rate-limit failures are returned as a
:class:`ReleaseFetchError` for the caller to record, never raised past the
service layer.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from sum_server.settings import get_settings

log = structlog.get_logger(__name__)

_TIMEOUT = httpx.Timeout(15.0, connect=8.0)


class ReleaseFetchError(Exception):
    """GitHub was unreachable, rate-limited, or returned no usable release."""


@dataclass(frozen=True)
class ReleaseInfo:
    version: str  # normalized without leading "v"
    tag: str
    name: str
    notes: str
    published_at: dt.datetime | None
    assets: list[dict[str, Any]] = field(default_factory=list)


def _normalize_version(tag: str) -> str:
    return tag[1:] if tag.startswith("v") else tag


def release_from_response(resp: httpx.Response) -> ReleaseInfo:
    """Map a GitHub ``releases/latest`` response to a :class:`ReleaseInfo`.

    Raises :class:`ReleaseFetchError` for error statuses or an unusable body.
    Pure (no network) so it is unit-testable with a constructed response.
    """
    if resp.status_code == 404:
        raise ReleaseFetchError("no releases found")
    if resp.status_code == 403:
        raise ReleaseFetchError("github rate limited")
    if resp.status_code >= 400:
        raise ReleaseFetchError(f"github returned {resp.status_code}")
    body = resp.json()
    tag = body.get("tag_name")
    if not tag:
        raise ReleaseFetchError("release has no tag")
    published = body.get("published_at")
    published_at = (
        dt.datetime.fromisoformat(published.replace("Z", "+00:00")) if published else None
    )
    assets = [
        {"name": a["name"], "url": a["browser_download_url"], "size": a.get("size", 0)}
        for a in body.get("assets", [])
    ]
    return ReleaseInfo(
        version=_normalize_version(tag),
        tag=tag,
        name=body.get("name") or tag,
        notes=body.get("body") or "",
        published_at=published_at,
        assets=assets,
    )


async def fetch_latest_release(repo: str) -> ReleaseInfo:
    """Fetch the latest release for ``owner/repo``.

    ``repo`` is the bare repository name; the owner comes from settings.
    """
    settings = get_settings()
    url = f"https://api.github.com/repos/{settings.github_owner}/{repo}/releases/latest"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "sum-server"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise ReleaseFetchError(f"github unreachable: {exc.__class__.__name__}") from exc
    return release_from_response(resp)
