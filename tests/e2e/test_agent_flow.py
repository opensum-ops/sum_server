"""End-to-end agent flow.

admin → create server → enrollment → agent enrolls → submit inventory →
admin creates a signed job → agent polls → picks up → reports result →
admin queries audit and sees every state change.
"""

from __future__ import annotations

import base64

from httpx import AsyncClient

from tests.conftest import auth_h


async def test_full_agent_flow(client: AsyncClient, admin_token: str) -> None:
    # 1. Create server
    sr = await client.post(
        "/api/v1/servers",
        headers=auth_h(admin_token),
        json={"name": "node-1", "status": "active"},
    )
    assert sr.status_code == 201
    server_id = sr.json()["id"]

    # 2. Create enrollment
    er = await client.post(
        "/api/v1/agents/enrollments",
        headers=auth_h(admin_token),
        json={"server_id": server_id},
    )
    assert er.status_code == 201
    enrollment_token = er.json()["enrollment_token"]

    # 3. Agent enrolls
    enroll = await client.post("/api/v1/agents/enroll", json={"enrollment_token": enrollment_token})
    assert enroll.status_code == 200
    agent_token = enroll.json()["agent_token"]
    pubkey_b64 = enroll.json()["signing_public_key"]
    assert enroll.json()["server_id"] == server_id

    # 4. Submit inventory
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

    # 5. Admin creates a signed job
    cr = await client.post(
        f"/api/v1/servers/{server_id}/jobs",
        headers=auth_h(admin_token),
        json={
            "capability": "rename_nic",
            "payload": {"current_name": "eth0", "new_name": "ens1"},
        },
    )
    assert cr.status_code == 201, cr.text
    job_id = cr.json()["id"]
    sig_b64 = cr.json()["signature"]

    # 6. Agent polls and finds the job
    poll = await client.get("/api/v1/agents/jobs", headers=auth_h(agent_token))
    assert poll.status_code == 200
    jobs = poll.json()["jobs"]
    assert any(j["id"] == job_id for j in jobs)

    # 7. Sanity-check the signature material the server published
    sig = base64.b64decode(sig_b64)
    pub = base64.b64decode(pubkey_b64)
    assert len(sig) == 64
    assert len(pub) == 32

    # 8. Agent picks up the job
    pu = await client.post(f"/api/v1/agents/jobs/{job_id}/pickup", headers=auth_h(agent_token))
    assert pu.status_code == 200
    assert pu.json()["status"] == "picked_up"

    # 9. Agent reports result
    res = await client.post(
        f"/api/v1/agents/jobs/{job_id}/result",
        headers=auth_h(agent_token),
        json={"status": "completed", "exit_code": 0, "output": {"renamed": True}},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "completed"

    # 10. Admin queries audit and sees every transition
    audit = await client.get("/api/v1/audit", headers=auth_h(admin_token))
    assert audit.status_code == 200
    actions = {e["action"] for e in audit.json()["items"]}
    for expected in {
        "server.create",
        "agent.enrollment_created",
        "agent.enrolled",
        "agent.inventory_submitted",
        "job.create",
        "job.picked_up",
        "job.completed",
    }:
        assert expected in actions, f"missing audit action: {expected}"
