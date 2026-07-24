"""Server self-update API: guards, request→queue→launch, status polling."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from sum_server.updates import system as system_svc
from tests.conftest import auth_h


@pytest.fixture
def _available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the deployment can self-update, and stub the launcher."""
    monkeypatch.setattr(system_svc, "self_update_available", lambda: (True, ""))

    async def fake_launch() -> None:
        return None

    monkeypatch.setattr(system_svc, "launch_updater", fake_launch)


@pytest.mark.usefixtures("_available")
async def test_start_update_queues_and_reports(client: AsyncClient, admin_token: str) -> None:
    r = await client.post(
        "/api/v1/system/update",
        headers=auth_h(admin_token),
        json={"target_version": "99.0.0"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["to_version"] == "99.0.0"
    assert body["status"] == "queued"

    # Status endpoint reflects the queued row.
    s = await client.get("/api/v1/system/update/status", headers=auth_h(admin_token))
    assert s.status_code == 200
    assert s.json()["to_version"] == "99.0.0"


@pytest.mark.usefixtures("_available")
async def test_rejects_non_newer_target(client: AsyncClient, admin_token: str) -> None:
    r = await client.post(
        "/api/v1/system/update", headers=auth_h(admin_token), json={"target_version": "0.0.1"}
    )
    assert r.status_code == 409
    assert "not newer" in r.json()["error"]["message"]


@pytest.mark.usefixtures("_available")
async def test_rejects_concurrent_update(client: AsyncClient, admin_token: str) -> None:
    first = await client.post(
        "/api/v1/system/update", headers=auth_h(admin_token), json={"target_version": "99.0.0"}
    )
    assert first.status_code == 200
    second = await client.post(
        "/api/v1/system/update", headers=auth_h(admin_token), json={"target_version": "99.1.0"}
    )
    assert second.status_code == 409
    assert "in progress" in second.json()["error"]["message"]


async def test_unavailable_when_not_configured(
    client: AsyncClient, admin_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        system_svc, "self_update_available", lambda: (False, "SUM_SERVER_INSTALL_DIR is not set")
    )
    r = await client.post(
        "/api/v1/system/update", headers=auth_h(admin_token), json={"target_version": "99.0.0"}
    )
    assert r.status_code == 409
    assert "unavailable" in r.json()["error"]["message"]


@pytest.mark.usefixtures("_available")
async def test_update_is_admin_only(client: AsyncClient, user_token: str) -> None:
    r = await client.post(
        "/api/v1/system/update", headers=auth_h(user_token), json={"target_version": "99.0.0"}
    )
    assert r.status_code == 403


async def test_status_404_when_none(client: AsyncClient, admin_token: str) -> None:
    r = await client.get("/api/v1/system/update/status", headers=auth_h(admin_token))
    assert r.status_code == 404


async def test_update_requested_is_audited(
    client: AsyncClient, admin_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(system_svc, "self_update_available", lambda: (True, ""))

    async def fake_launch() -> None:
        return None

    monkeypatch.setattr(system_svc, "launch_updater", fake_launch)
    await client.post(
        "/api/v1/system/update", headers=auth_h(admin_token), json={"target_version": "99.0.0"}
    )
    audit = await client.get(
        "/api/v1/audit", headers=auth_h(admin_token), params={"action": "system.update_requested"}
    )
    assert len(audit.json()["items"]) == 1
