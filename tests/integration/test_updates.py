"""Update-check API: summary + manual check with GitHub mocked."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from httpx import AsyncClient

from sum_server import __version__
from sum_server.updates import service as updates_svc
from sum_server.updates.github import ReleaseInfo
from tests.conftest import auth_h


def _fake_release(version: str) -> ReleaseInfo:
    return ReleaseInfo(
        version=version,
        tag=f"v{version}",
        name=f"v{version}",
        notes=f"notes for {version}",
        published_at=dt.datetime(2026, 7, 24, tzinfo=dt.UTC),
        assets=[],
    )


@pytest.fixture
def mock_github(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch(repo: str) -> ReleaseInfo:
        # Advertise a newer server and a newer agent than whatever is running.
        return _fake_release("9.9.9")

    monkeypatch.setattr(updates_svc, "fetch_latest_release", fake_fetch)


@pytest.mark.usefixtures("mock_github")
async def test_check_populates_and_reports_available(
    client: AsyncClient, admin_token: str
) -> None:
    r = await client.post("/api/v1/updates/check", headers=auth_h(admin_token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["server"]["current_version"] == __version__
    assert body["server"]["latest_version"] == "9.9.9"
    assert body["server"]["update_available"] is True
    assert "notes for 9.9.9" in body["server"]["notes"]
    assert body["agent"]["latest_version"] == "9.9.9"


@pytest.mark.usefixtures("mock_github")
async def test_summary_reads_cache(client: AsyncClient, admin_token: str) -> None:
    await client.post("/api/v1/updates/check", headers=auth_h(admin_token))
    r = await client.get("/api/v1/updates", headers=auth_h(admin_token))
    assert r.status_code == 200
    assert r.json()["server"]["latest_version"] == "9.9.9"


async def test_check_is_admin_only(client: AsyncClient, user_token: str) -> None:
    r = await client.post("/api/v1/updates/check", headers=auth_h(user_token))
    assert r.status_code == 403


async def test_offline_records_error_not_crash(
    client: AsyncClient, admin_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sum_server.updates.github import ReleaseFetchError

    async def boom(repo: str) -> ReleaseInfo:
        raise ReleaseFetchError("github unreachable: ConnectError")

    monkeypatch.setattr(updates_svc, "fetch_latest_release", boom)
    r = await client.post("/api/v1/updates/check", headers=auth_h(admin_token))
    assert r.status_code == 200
    body: dict[str, Any] = r.json()
    assert body["server"]["latest_version"] is None
    assert "unreachable" in body["server"]["error"]
    assert body["server"]["update_available"] is False
