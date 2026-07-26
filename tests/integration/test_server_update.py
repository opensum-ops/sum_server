"""Server self-update API: guards, request→queue→launch, status polling."""

from __future__ import annotations

import datetime as dt
from typing import Any

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


async def test_failed_launch_does_not_block_the_next_attempt(
    client: AsyncClient, admin_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launch that blows up must terminate its row, not strand it as queued.

    A stranded non-terminal row would trip the concurrency guard forever, so a
    single bad launch would disable self-update until someone edited the table
    by hand.
    """
    monkeypatch.setattr(system_svc, "self_update_available", lambda: (True, ""))

    async def boom() -> None:
        raise RuntimeError("systemd-run exploded")

    monkeypatch.setattr(system_svc, "launch_updater", boom)
    first = await client.post(
        "/api/v1/system/update", headers=auth_h(admin_token), json={"target_version": "99.0.0"}
    )
    assert first.status_code == 409
    assert "could not launch updater" in first.json()["error"]["message"]

    status = await client.get("/api/v1/system/update/status", headers=auth_h(admin_token))
    assert status.json()["status"] == "failed"
    assert "launch failed" in status.json()["detail"]

    # The next attempt gets through rather than hitting "already in progress".
    async def fake_launch() -> None:
        return None

    monkeypatch.setattr(system_svc, "launch_updater", fake_launch)
    second = await client.post(
        "/api/v1/system/update", headers=auth_h(admin_token), json={"target_version": "99.0.0"}
    )
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "queued"


@pytest.mark.usefixtures("_available")
async def test_abandoned_queued_row_is_reaped(
    client: AsyncClient, admin_token: str, db_session: Any
) -> None:
    """The shape left behind when an updater dies before its first DB write."""
    from sqlalchemy import select

    from sum_server.updates.models import ServerUpdate

    first = await client.post(
        "/api/v1/system/update", headers=auth_h(admin_token), json={"target_version": "99.0.0"}
    )
    assert first.status_code == 200

    # Backdate it past the abandonment window without ever setting started_at.
    row = (await db_session.execute(select(ServerUpdate))).scalars().one()
    row.created_at = dt.datetime.now(tz=dt.UTC) - dt.timedelta(
        seconds=system_svc.ABANDONED_QUEUED_SECONDS + 60
    )
    await db_session.commit()

    second = await client.post(
        "/api/v1/system/update", headers=auth_h(admin_token), json={"target_version": "99.1.0"}
    )
    assert second.status_code == 200, second.text
    assert second.json()["to_version"] == "99.1.0"

    audit = await client.get(
        "/api/v1/audit", headers=auth_h(admin_token), params={"action": "system.update_abandoned"}
    )
    assert len(audit.json()["items"]) == 1


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
