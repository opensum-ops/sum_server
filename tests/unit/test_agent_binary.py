"""Agent binary caching: download + checksum verification (httpx mocked)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from sum_server.settings import get_settings
from sum_server.updates import agent_binary
from sum_server.updates.agent_binary import BinaryUnavailableError, ensure_cached
from sum_server.updates.models import COMPONENT_AGENT, ReleaseCache

BIN = b"\x7fELF fake agent binary payload"
GOOD_SHA = hashlib.sha256(BIN).hexdigest()
VERSION = "0.3.0"
NAME = f"sum-agent-v{VERSION}-linux-amd64"


class _FakeSession:
    """Minimal stand-in returning a ReleaseCache for the agent repo."""

    def __init__(self, cache: ReleaseCache | None) -> None:
        self._cache = cache

    async def execute(self, *_a: object, **_k: object) -> object:  # pragma: no cover
        raise AssertionError("ensure_cached should use get_release_cache, monkeypatched")


def _mock_transport(sha_body: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".sha256"):
            return httpx.Response(200, text=sha_body)
        return httpx.Response(200, content=BIN)

    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def _tmp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)


def _patch_http(monkeypatch: pytest.MonkeyPatch, sha_body: str) -> None:
    transport = _mock_transport(sha_body)
    real = httpx.AsyncClient

    def factory(**kwargs: object) -> httpx.AsyncClient:
        kwargs.pop("follow_redirects", None)
        kwargs.pop("timeout", None)
        return real(transport=transport)

    monkeypatch.setattr(agent_binary.httpx, "AsyncClient", factory)


def _cache(assets: list[dict[str, object]]) -> ReleaseCache:
    return ReleaseCache(repo=COMPONENT_AGENT, latest_version=VERSION, assets=assets)


async def test_ensure_cached_downloads_and_verifies(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _cache(
        [
            {"name": NAME, "url": "https://x/bin"},
            {"name": f"{NAME}.sha256", "url": "https://x/bin.sha256"},
        ]
    )

    async def fake_get_cache(_s: object, _repo: str) -> ReleaseCache:
        return cache

    monkeypatch.setattr(agent_binary, "get_release_cache", fake_get_cache)
    _patch_http(monkeypatch, f"{GOOD_SHA}  {NAME}")

    result = await ensure_cached(_FakeSession(cache), VERSION)  # type: ignore[arg-type]
    assert result.sha256 == GOOD_SHA
    assert result.path.read_bytes() == BIN
    # Sidecar written for cheap re-reads.
    assert result.path.with_suffix(".sha256").read_text().strip() == GOOD_SHA
    assert agent_binary.cached_binary_if_present(VERSION) is not None


async def test_ensure_cached_rejects_bad_checksum(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _cache(
        [
            {"name": NAME, "url": "https://x/bin"},
            {"name": f"{NAME}.sha256", "url": "https://x/bin.sha256"},
        ]
    )

    async def fake_get_cache(_s: object, _repo: str) -> ReleaseCache:
        return cache

    monkeypatch.setattr(agent_binary, "get_release_cache", fake_get_cache)
    _patch_http(monkeypatch, "0" * 64)  # wrong hash

    with pytest.raises(BinaryUnavailableError, match="checksum mismatch"):
        await ensure_cached(_FakeSession(cache), VERSION)  # type: ignore[arg-type]
    assert agent_binary.cached_binary_if_present(VERSION) is None


async def test_ensure_cached_unknown_version(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _cache([])

    async def fake_get_cache(_s: object, _repo: str) -> ReleaseCache:
        return cache

    monkeypatch.setattr(agent_binary, "get_release_cache", fake_get_cache)
    with pytest.raises(BinaryUnavailableError, match="not the known latest"):
        await ensure_cached(_FakeSession(cache), "1.2.3")  # type: ignore[arg-type]
