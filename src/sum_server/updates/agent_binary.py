"""Cache agent binaries the server serves to agents.

The server downloads the agent binary + its ``.sha256`` from the GitHub
release (once, verifying the hash) into ``data_dir/agent-binaries/<version>/``
and serves it to agents over TLS. Agents never contact GitHub.

MVP scope: linux/amd64 only, and only the version currently in the agent
release cache (the "latest" the server knows about) — which is exactly what
the host-page button offers.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from sum_server.settings import get_settings
from sum_server.updates.models import COMPONENT_AGENT
from sum_server.updates.service import get_release_cache

log = structlog.get_logger(__name__)

_ARCH_SUFFIX = "linux-amd64"
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class BinaryUnavailableError(Exception):
    """The requested agent binary could not be cached (missing asset, bad hash)."""


@dataclass(frozen=True)
class CachedBinary:
    version: str
    path: Path
    sha256: str


def _binary_name(version: str) -> str:
    return f"sum-agent-v{version}-{_ARCH_SUFFIX}"


def _cache_path(version: str) -> Path:
    data_dir = get_settings().data_dir
    return data_dir / "agent-binaries" / version / f"sum-agent-{_ARCH_SUFFIX}"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    async with (
        httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client,
        client.stream("GET", url) as resp,
    ):
        resp.raise_for_status()
        with tmp.open("wb") as f:
            async for chunk in resp.aiter_bytes():
                f.write(chunk)
    tmp.replace(dest)


async def ensure_cached(session: AsyncSession, version: str) -> CachedBinary:
    """Return the cached binary for ``version``, downloading it if needed."""
    path = _cache_path(version)
    expected_name = _binary_name(version)

    # Read everything needed into plain values, then release the transaction
    # SQLAlchemy autobegan for that read. Two reasons to let it go: the caller
    # owns the write transaction and cannot open it while ours is still on the
    # session (the discipline set on 2026-07-07), and everything below is slow
    # network I/O that must not pin a database connection for the length of a
    # 20MB download. Values are copied out first because rollback expires the
    # ORM instance, and touching an expired attribute afterwards would try to
    # refresh it from a connection we no longer hold.
    cache = await get_release_cache(session, COMPONENT_AGENT)
    latest_version = cache.latest_version if cache is not None else None
    assets = {a["name"]: a["url"] for a in cache.assets} if cache is not None else {}
    await session.rollback()

    if latest_version != version:
        raise BinaryUnavailableError(
            f"agent {version} is not the known latest release; refresh update check first"
        )
    if expected_name not in assets or f"{expected_name}.sha256" not in assets:
        raise BinaryUnavailableError(f"release has no {_ARCH_SUFFIX} asset for {version}")

    # Fetch the published checksum first.
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        try:
            sha_resp = await client.get(assets[f"{expected_name}.sha256"])
            sha_resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise BinaryUnavailableError(f"could not fetch checksum: {exc}") from exc
    expected_sha = sha_resp.text.strip().split()[0]

    if path.exists() and _sha256_file(path) == expected_sha:
        return CachedBinary(version=version, path=path, sha256=expected_sha)

    try:
        await _download(assets[expected_name], path)
    except httpx.HTTPError as exc:
        raise BinaryUnavailableError(f"could not download binary: {exc}") from exc

    actual = _sha256_file(path)
    if actual != expected_sha:
        path.unlink(missing_ok=True)
        raise BinaryUnavailableError(
            f"checksum mismatch for {version}: expected {expected_sha}, got {actual}"
        )
    path.with_suffix(".sha256").write_text(actual, encoding="utf-8")
    log.info("agent_binary_cached", version=version, sha256=actual)
    return CachedBinary(version=version, path=path, sha256=actual)


def cached_binary_if_present(version: str) -> CachedBinary | None:
    """Return a cached binary (path + sha from the sidecar) or ``None``.

    Cheap: reads the stored checksum rather than re-hashing the binary.
    """
    path = _cache_path(version)
    sidecar = path.with_suffix(".sha256")
    if not path.exists() or not sidecar.exists():
        return None
    return CachedBinary(
        version=version, path=path, sha256=sidecar.read_text(encoding="utf-8").strip()
    )
