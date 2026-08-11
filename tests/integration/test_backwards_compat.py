"""Best-effort N-1 backwards compatibility: an older agent still works.

Simulates a 0.2.0-shaped agent against the current server: an old User-Agent
and request bodies that omit the newest additive fields (``agent_version``).
The server must accept them unchanged.
"""

from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import auth_h

OLD_UA = {"User-Agent": "sum-agent/0.2.0"}


async def _enroll(client: AsyncClient, admin_token: str) -> str:
    hr = await client.post(
        "/api/v1/hosts",
        headers=auth_h(admin_token),
        json={"hostname": "compat", "status": "active"},
    )
    host_id = hr.json()["id"]
    er = await client.post(
        "/api/v1/agents/enrollments", headers=auth_h(admin_token), json={"host_id": host_id}
    )
    en = await client.post(
        "/api/v1/agents/enroll", json={"enrollment_token": er.json()["enrollment_token"]}
    )
    return str(en.json()["agent_token"])


async def test_old_agent_inventory_and_heartbeat(client: AsyncClient, admin_token: str) -> None:
    agent = await _enroll(client, admin_token)
    h = {**auth_h(agent), **OLD_UA}

    # Inventory body as a 0.2.0 agent sends it: no top-level agent_version field.
    inv = await client.post(
        "/api/v1/agents/inventory",
        headers=h,
        json={
            "facts": {"hostname": "compat.local", "agent_version": "0.2.0"},
            "components": [],
        },
    )
    assert inv.status_code == 200, inv.text
    assert inv.json()["facts_created"] == 2

    # Heartbeat body as a 0.2.0 agent sends it: no agent_version field.
    hb = await client.post(
        "/api/v1/agents/heartbeat", headers=h, json={"state": "running", "boot_id": "b1"}
    )
    assert hb.status_code == 200, hb.text
    assert hb.json()["presence"] == "online"

    # The server has not learned to require anything the old agent omits.
    hb2 = await client.post("/api/v1/agents/heartbeat", headers=h, json={"state": "running"})
    assert hb2.status_code == 200
