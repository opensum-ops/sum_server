"""End-to-end agent flow.

admin → create host → enrollment → agent enrolls → submit inventory →
resubmit with a swapped disk → admin queries audit and sees every state change.
"""

from __future__ import annotations

import base64

from httpx import AsyncClient

from tests.conftest import auth_h


async def test_full_agent_flow(client: AsyncClient, admin_token: str) -> None:
    # 1. Create host
    sr = await client.post(
        "/api/v1/hosts",
        headers=auth_h(admin_token),
        json={"name": "node-1", "status": "active"},
    )
    assert sr.status_code == 201
    host_id = sr.json()["id"]

    # 2. Create enrollment
    er = await client.post(
        "/api/v1/agents/enrollments",
        headers=auth_h(admin_token),
        json={"host_id": host_id},
    )
    assert er.status_code == 201
    enrollment_token = er.json()["enrollment_token"]

    # 3. Agent enrolls
    enroll = await client.post("/api/v1/agents/enroll", json={"enrollment_token": enrollment_token})
    assert enroll.status_code == 200
    agent_token = enroll.json()["agent_token"]
    pubkey_b64 = enroll.json()["signing_public_key"]
    assert enroll.json()["host_id"] == host_id

    # 4. The published signing key is a valid Ed25519 public key
    assert len(base64.b64decode(pubkey_b64)) == 32

    # 5. Submit inventory
    inv = await client.post(
        "/api/v1/agents/inventory",
        headers=auth_h(agent_token),
        json={
            "components": [
                {
                    "kind": "disk",
                    "vendor": "Samsung",
                    "model": "PM9A3",
                    "serial": "S6KZNX0T123456",
                    "slot": "nvme0",
                    "attrs": {
                        "kind": "disk",
                        "size_bytes": 1_920_000_000_000,
                        "rotation_rpm": 0,
                        "bus": "nvme",
                    },
                },
                {
                    "kind": "cpu",
                    "vendor": "AMD",
                    "model": "EPYC 7763",
                    "serial": "CPU-0",
                    "attrs": {
                        "kind": "cpu",
                        "cores": 64,
                        "threads": 128,
                        "base_hz": 2_450_000_000,
                    },
                },
            ]
        },
    )
    assert inv.status_code == 200, inv.text
    assert inv.json()["created"] == 2

    # 6. Resubmit with a different disk in the same slot: swap detected
    inv2 = await client.post(
        "/api/v1/agents/inventory",
        headers=auth_h(agent_token),
        json={
            "components": [
                {
                    "kind": "disk",
                    "vendor": "Samsung",
                    "model": "PM9A3",
                    "serial": "S6KZNX0T999999",
                    "slot": "nvme0",
                    "attrs": {
                        "kind": "disk",
                        "size_bytes": 1_920_000_000_000,
                        "rotation_rpm": 0,
                        "bus": "nvme",
                    },
                },
                {
                    "kind": "cpu",
                    "vendor": "AMD",
                    "model": "EPYC 7763",
                    "serial": "CPU-0",
                    "attrs": {
                        "kind": "cpu",
                        "cores": 64,
                        "threads": 128,
                        "base_hz": 2_450_000_000,
                    },
                },
            ]
        },
    )
    assert inv2.status_code == 200, inv2.text
    assert inv2.json()["swaps"] == 1
    assert inv2.json()["created"] == 1

    # 7. Components reflect the swap: old disk absent, new disk present
    comps = await client.get(
        f"/api/v1/hosts/{host_id}/components",
        headers=auth_h(admin_token),
        params={"include_absent": "true"},
    )
    assert comps.status_code == 200
    disks = {c["serial"]: c["present"] for c in comps.json() if c["kind"] == "disk"}
    assert disks == {"S6KZNX0T123456": False, "S6KZNX0T999999": True}

    # 8. Admin queries audit and sees every transition
    audit = await client.get("/api/v1/audit", headers=auth_h(admin_token))
    assert audit.status_code == 200
    actions = {e["action"] for e in audit.json()["items"]}
    for expected in {
        "host.create",
        "agent.enrollment_created",
        "agent.enrolled",
        "agent.inventory_submitted",
        "host.component_swap",
    }:
        assert expected in actions, f"missing audit action: {expected}"
