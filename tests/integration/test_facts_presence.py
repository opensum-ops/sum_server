"""Facts ingest + heartbeat/presence integration tests (API level)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from httpx import AsyncClient

from tests.conftest import auth_h


async def _enrolled_host(client: AsyncClient, admin_token: str) -> tuple[str, str]:
    """Create a host + enrollment, enroll, return ``(host_id, agent_token)``."""
    hr = await client.post(
        "/api/v1/hosts",
        headers=auth_h(admin_token),
        json={"hostname": "fp-node", "status": "provisioning"},
    )
    assert hr.status_code == 201, hr.text
    host_id = hr.json()["id"]
    er = await client.post(
        "/api/v1/agents/enrollments",
        headers=auth_h(admin_token),
        json={"host_id": host_id},
    )
    assert er.status_code == 201
    en = await client.post(
        "/api/v1/agents/enroll",
        json={"enrollment_token": er.json()["enrollment_token"]},
    )
    assert en.status_code == 200
    return host_id, en.json()["agent_token"]


async def _get_host(client: AsyncClient, admin_token: str, host_id: str) -> dict[str, Any]:
    r = await client.get(f"/api/v1/hosts/{host_id}", headers=auth_h(admin_token))
    assert r.status_code == 200
    body: dict[str, Any] = r.json()
    return body


async def test_enroll_activates_and_heartbeat_goes_online(
    client: AsyncClient, admin_token: str
) -> None:
    host_id, agent_token = await _enrolled_host(client, admin_token)

    # Enrollment flipped provisioning -> active; no heartbeat yet -> pending.
    host = await _get_host(client, admin_token, host_id)
    assert host["status"] == "active"
    assert host["presence"] == "pending"

    hb = await client.post(
        "/api/v1/agents/heartbeat",
        headers=auth_h(agent_token),
        json={"state": "running", "boot_id": "boot-1"},
    )
    assert hb.status_code == 200, hb.text
    assert hb.json()["presence"] == "online"

    host = await _get_host(client, admin_token, host_id)
    assert host["presence"] == "online"
    assert host["last_heartbeat_at"] is not None


async def test_goodbye_states(client: AsyncClient, admin_token: str) -> None:
    host_id, agent_token = await _enrolled_host(client, admin_token)

    for detail, expected in [
        ("rebooting", "rebooting"),
        ("powered_off", "powered_off"),
        ("agent_stop", "stopped"),
    ]:
        hb = await client.post(
            "/api/v1/agents/heartbeat",
            headers=auth_h(agent_token),
            json={"state": "stopping", "detail": detail},
        )
        assert hb.status_code == 200
        assert hb.json()["presence"] == expected
        host = await _get_host(client, admin_token, host_id)
        assert host["presence"] == expected

    # A running heartbeat clears the goodbye.
    hb = await client.post(
        "/api/v1/agents/heartbeat", headers=auth_h(agent_token), json={"state": "running"}
    )
    assert hb.json()["presence"] == "online"

    # Shutdown reports are audited.
    audit = await client.get("/api/v1/audit", headers=auth_h(admin_token))
    actions = [e["action"] for e in audit.json()["items"]]
    assert actions.count("host.reported_shutdown") == 3


async def test_stale_heartbeat_is_unreachable(
    client: AsyncClient, admin_token: str, db_session: Any
) -> None:
    host_id, agent_token = await _enrolled_host(client, admin_token)
    hb = await client.post(
        "/api/v1/agents/heartbeat", headers=auth_h(agent_token), json={"state": "running"}
    )
    assert hb.status_code == 200

    # Age the heartbeat past the online window directly in the DB.
    from sqlalchemy import update

    from sum_server.hosts.models import Host

    await db_session.execute(
        update(Host)
        .where(Host.id.in_([host_id]))
        .values(last_heartbeat_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=600))
    )
    await db_session.commit()

    host = await _get_host(client, admin_token, host_id)
    assert host["presence"] == "unreachable"


async def test_unclean_reboot_audited(client: AsyncClient, admin_token: str) -> None:
    _host_id, agent_token = await _enrolled_host(client, admin_token)
    for boot_id in ("boot-1", "boot-1"):
        r = await client.post(
            "/api/v1/agents/heartbeat",
            headers=auth_h(agent_token),
            json={"state": "running", "boot_id": boot_id},
        )
        assert r.status_code == 200

    # boot_id changes without a goodbye: crash detected.
    r = await client.post(
        "/api/v1/agents/heartbeat",
        headers=auth_h(agent_token),
        json={"state": "running", "boot_id": "boot-2"},
    )
    assert r.status_code == 200

    audit = await client.get(
        "/api/v1/audit", headers=auth_h(admin_token), params={"action": "host.unclean_reboot"}
    )
    items = audit.json()["items"]
    assert len(items) == 1
    assert items[0]["payload"] == {"old_boot_id": "boot-1", "new_boot_id": "boot-2"}

    # A boot_id change after a clean reboot goodbye is not flagged.
    r = await client.post(
        "/api/v1/agents/heartbeat",
        headers=auth_h(agent_token),
        json={"state": "stopping", "detail": "rebooting"},
    )
    r = await client.post(
        "/api/v1/agents/heartbeat",
        headers=auth_h(agent_token),
        json={"state": "running", "boot_id": "boot-3"},
    )
    audit = await client.get(
        "/api/v1/audit", headers=auth_h(admin_token), params={"action": "host.unclean_reboot"}
    )
    assert len(audit.json()["items"]) == 1


async def test_facts_ingest_lifecycle(client: AsyncClient, admin_token: str) -> None:
    host_id, agent_token = await _enrolled_host(client, admin_token)

    inv = await client.post(
        "/api/v1/agents/inventory",
        headers=auth_h(agent_token),
        json={
            "facts": {
                "hostname": "node-1.example.com",
                "kernel": "6.9.3-x64v3",
                "os_name": "Debian GNU/Linux",
                "cpu_count": 16,
            },
            "components": [],
        },
    )
    assert inv.status_code == 200, inv.text
    body = inv.json()
    assert body["facts_created"] == 4
    assert body["facts_updated"] == 0
    assert body["facts_removed"] == 0

    # Hostname is adopted onto the host row.
    host = await _get_host(client, admin_token, host_id)
    assert host["hostname"] == "node-1.example.com"

    # Facts read API.
    fr = await client.get(f"/api/v1/hosts/{host_id}/facts", headers=auth_h(admin_token))
    assert fr.status_code == 200
    facts = {f["key"]: f["value"] for f in fr.json()}
    assert facts["kernel"] == "6.9.3-x64v3"
    assert facts["cpu_count"] == 16

    # Update one, drop one; snapshot semantics remove missing keys.
    inv2 = await client.post(
        "/api/v1/agents/inventory",
        headers=auth_h(agent_token),
        json={
            "facts": {
                "hostname": "node-1.example.com",
                "kernel": "6.10.0-x64v3",
                "os_name": "Debian GNU/Linux",
            },
            "components": [],
        },
    )
    assert inv2.status_code == 200
    body2 = inv2.json()
    assert body2["facts_updated"] == 1
    assert body2["facts_removed"] == 1

    fr2 = await client.get(f"/api/v1/hosts/{host_id}/facts", headers=auth_h(admin_token))
    facts2 = {f["key"]: f["value"] for f in fr2.json()}
    assert facts2["kernel"] == "6.10.0-x64v3"
    assert "cpu_count" not in facts2


async def test_invalid_fact_key_rejected(client: AsyncClient, admin_token: str) -> None:
    _host_id, agent_token = await _enrolled_host(client, admin_token)
    inv = await client.post(
        "/api/v1/agents/inventory",
        headers=auth_h(agent_token),
        json={"facts": {"Bad Key!": "x"}, "components": []},
    )
    assert inv.status_code == 422
