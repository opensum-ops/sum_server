"""GitHub release response parsing + error mapping (no network)."""

from __future__ import annotations

import httpx
import pytest

from sum_server.updates.github import ReleaseFetchError, release_from_response

_RELEASE_BODY = {
    "tag_name": "v0.3.0",
    "name": "v0.3.0 - self update",
    "body": "## Notes\n- did things",
    "published_at": "2026-07-24T12:00:00Z",
    "assets": [
        {
            "name": "sum-agent-v0.3.0-linux-amd64",
            "browser_download_url": "https://example/dl/bin",
            "size": 12345,
        }
    ],
}


def _resp(status: int, json: object) -> httpx.Response:
    return httpx.Response(status_code=status, json=json, request=httpx.Request("GET", "http://x"))


def test_parse_success() -> None:
    info = release_from_response(_resp(200, _RELEASE_BODY))
    assert info.version == "0.3.0"  # leading v stripped
    assert info.tag == "v0.3.0"
    assert info.name == "v0.3.0 - self update"
    assert info.notes.startswith("## Notes")
    assert info.published_at is not None
    assert info.assets[0]["name"] == "sum-agent-v0.3.0-linux-amd64"
    assert info.assets[0]["url"] == "https://example/dl/bin"


def test_404_no_releases() -> None:
    with pytest.raises(ReleaseFetchError, match="no releases"):
        release_from_response(_resp(404, {}))


def test_403_rate_limited() -> None:
    with pytest.raises(ReleaseFetchError, match="rate limited"):
        release_from_response(_resp(403, {}))


def test_500_generic() -> None:
    with pytest.raises(ReleaseFetchError, match="github returned 500"):
        release_from_response(_resp(500, {}))


def test_missing_tag() -> None:
    with pytest.raises(ReleaseFetchError, match="no tag"):
        release_from_response(_resp(200, {"name": "x"}))
