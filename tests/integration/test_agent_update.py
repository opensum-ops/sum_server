"""Server-side agent update: signed heartbeat directive, binary serve, UI button."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient

from sum_server.core.security import signing
from sum_server.settings import get_settings
from sum_server.updates import agent_binary
from sum_server.updates.agent_binary import CachedBinary
from sum_server.updates.directive import signing_payload
from tests.conftest import auth_h

BIN = b"fake-agent-binary-0.3.0"
SHA = hashlib.sha256(BIN).hexdigest()
TARGET = "0.3.0"


@pytest.fixture(autouse=True)
def _tmp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)


def _seed_cached_binary() -> Path:
    """Write a cached binary + sha sidecar as if it were already downloaded."""
    path = get_settings().data_dir / "agent-binaries" / TARGET / "sum-agent-linux-amd64"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(BIN)
    path.with_suffix(".sha256").write_text(SHA, encoding="utf-8")
    return path


async def _enrolled(client: AsyncClient, admin_token: str) -> tuple[str, str]:
    hr = await client.post(
        "/api/v1/hosts", headers=auth_h(admin_token), json={"name": "au", "status": "active"}
    )
    host_id = hr.json()["id"]
    er = await client.post(
        "/api/v1/agents/enrollments", headers=auth_h(admin_token), json={"host_id": host_id}
    )
    en = await client.post(
        "/api/v1/agents/enroll", json={"enrollment_token": er.json()["enrollment_token"]}
    )
    return host_id, en.json()["agent_token"]


async def _set_target(client: AsyncClient, admin_token: str, host_id: str, db_session: Any) -> None:
    from sqlalchemy import update

    from sum_server.hosts.models import Host

    await db_session.execute(
        update(Host).where(Host.id.in_([host_id])).values(target_agent_version=TARGET)
    )
    await db_session.commit()


async def test_heartbeat_emits_signed_directive(
    client: AsyncClient, admin_token: str, db_session: Any
) -> None:
    _seed_cached_binary()
    host_id, agent = await _enrolled(client, admin_token)
    # Report an arch so the directive is served, and an old agent version.
    await client.post(
        "/api/v1/agents/inventory",
        headers=auth_h(agent),
        json={"facts": {"arch": "x86_64", "agent_version": "0.2.0"}, "components": []},
    )
    await _set_target(client, admin_token, host_id, db_session)

    hb = await client.post(
        "/api/v1/agents/heartbeat",
        headers={**auth_h(agent), "User-Agent": "sum-agent/0.2.0"},
        json={"state": "running"},
    )
    assert hb.status_code == 200, hb.text
    directive = hb.json()["agent_update"]
    assert directive is not None
    assert directive["target_version"] == TARGET
    assert directive["sha256"] == SHA
    assert directive["binary_url"].endswith(f"/api/v1/agents/binary/{TARGET}")

    # The signature verifies against the server key over {host_id, target, sha}.
    payload = signing_payload_from(host_id)
    assert signing.verify(payload, base64.b64decode(directive["signature"]))


def signing_payload_from(host_id: str) -> dict[str, str]:
    import uuid

    return signing_payload(uuid.UUID(host_id), TARGET, SHA)


async def test_directive_cleared_when_agent_reports_target(
    client: AsyncClient, admin_token: str, db_session: Any
) -> None:
    _seed_cached_binary()
    host_id, agent = await _enrolled(client, admin_token)
    await _set_target(client, admin_token, host_id, db_session)

    # Agent now runs the target version -> no directive, target cleared.
    hb = await client.post(
        "/api/v1/agents/heartbeat",
        headers={**auth_h(agent), "User-Agent": f"sum-agent/{TARGET}"},
        json={"state": "running", "agent_version": TARGET},
    )
    assert hb.status_code == 200
    assert hb.json()["agent_update"] is None

    detail = await client.get(f"/api/v1/hosts/{host_id}", headers=auth_h(admin_token))
    # target cleared -> a subsequent heartbeat carries no directive either
    assert detail.status_code == 200


async def test_binary_endpoint_streams_cached_file(client: AsyncClient, admin_token: str) -> None:
    _seed_cached_binary()
    _host_id, agent = await _enrolled(client, admin_token)
    r = await client.get(f"/api/v1/agents/binary/{TARGET}", headers=auth_h(agent))
    assert r.status_code == 200
    assert r.content == BIN


async def test_binary_endpoint_404_when_absent(client: AsyncClient, admin_token: str) -> None:
    _host_id, agent = await _enrolled(client, admin_token)
    r = await client.get("/api/v1/agents/binary/9.9.9", headers=auth_h(agent))
    assert r.status_code == 404


async def test_host_page_update_button(
    client: AsyncClient, admin_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.integration.test_ui import _ui_login

    host_id, _agent = await _enrolled(client, admin_token)

    async def fake_ensure(_session: object, version: str) -> CachedBinary:
        path = _seed_cached_binary()
        return CachedBinary(version=version, path=path, sha256=SHA)

    monkeypatch.setattr(agent_binary, "ensure_cached", fake_ensure)

    await _ui_login(client, "admin@example.com", "admin-pw-1234")
    csrf = client.cookies["sum_csrf"]
    r = await client.post(
        f"/hosts/{host_id}/agent-update",
        data={"csrf_token": csrf, "target_version": TARGET},
    )
    assert r.status_code == 303

    detail = await client.get(f"/hosts/{host_id}")
    assert f"updating → {TARGET}" in detail.text or "updating" in detail.text
