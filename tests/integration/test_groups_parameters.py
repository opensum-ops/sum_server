"""Groups + parameters API integration tests."""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient

from tests.conftest import auth_h


async def _mk_group(
    client: AsyncClient, token: str, name: str, parent_id: str | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"name": name}
    if parent_id is not None:
        body["parent_id"] = parent_id
    r = await client.post("/api/v1/groups", headers=auth_h(token), json=body)
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out


async def _mk_host(client: AsyncClient, token: str, name: str) -> str:
    r = await client.post(
        "/api/v1/hosts", headers=auth_h(token), json={"hostname": name, "status": "active"}
    )
    assert r.status_code == 201
    host_id: str = r.json()["id"]
    return host_id


async def _global_id(client: AsyncClient, token: str) -> str:
    r = await client.get("/api/v1/groups", headers=auth_h(token))
    assert r.status_code == 200
    gid: str = next(g["id"] for g in r.json() if g["name"] == "global")
    return gid


async def test_global_group_exists_and_is_protected(client: AsyncClient, admin_token: str) -> None:
    gid = await _global_id(client, admin_token)

    r = await client.patch(
        f"/api/v1/groups/{gid}", headers=auth_h(admin_token), json={"name": "renamed"}
    )
    assert r.status_code == 409
    r = await client.delete(f"/api/v1/groups/{gid}", headers=auth_h(admin_token))
    assert r.status_code == 409
    # No duplicate root.
    r = await client.post("/api/v1/groups", headers=auth_h(admin_token), json={"name": "global"})
    assert r.status_code == 409


async def test_group_tree_crud_and_cycle_guard(client: AsyncClient, admin_token: str) -> None:
    gid = await _global_id(client, admin_token)
    east = await _mk_group(client, admin_token, "dc-east")
    assert east["parent_id"] == gid  # parent defaults to global
    web = await _mk_group(client, admin_token, "web", parent_id=east["id"])

    # Reparenting under a descendant is refused.
    r = await client.patch(
        f"/api/v1/groups/{east['id']}",
        headers=auth_h(admin_token),
        json={"parent_id": web["id"]},
    )
    assert r.status_code == 409

    # Deleting a group with children is refused; leaf delete works.
    r = await client.delete(f"/api/v1/groups/{east['id']}", headers=auth_h(admin_token))
    assert r.status_code == 409
    r = await client.delete(f"/api/v1/groups/{web['id']}", headers=auth_h(admin_token))
    assert r.status_code == 204
    r = await client.delete(f"/api/v1/groups/{east['id']}", headers=auth_h(admin_token))
    assert r.status_code == 204


async def test_group_mutations_are_admin_only(
    client: AsyncClient, admin_token: str, user_token: str
) -> None:
    r = await client.post("/api/v1/groups", headers=auth_h(user_token), json={"name": "nope"})
    assert r.status_code == 403
    # Reads are fine for any user.
    r = await client.get("/api/v1/groups", headers=auth_h(user_token))
    assert r.status_code == 200


async def test_membership_and_effective_parameters(client: AsyncClient, admin_token: str) -> None:
    gid = await _global_id(client, admin_token)
    east = await _mk_group(client, admin_token, "dc-east")
    web = await _mk_group(client, admin_token, "web", parent_id=east["id"])
    host_id = await _mk_host(client, admin_token, "gp-node")

    # global membership is implicit and cannot be made explicit.
    r = await client.post(
        f"/api/v1/groups/{gid}/members",
        headers=auth_h(admin_token),
        json={"host_id": host_id},
    )
    assert r.status_code == 409

    r = await client.post(
        f"/api/v1/groups/{web['id']}/members",
        headers=auth_h(admin_token),
        json={"host_id": host_id},
    )
    assert r.status_code == 204
    r = await client.get(f"/api/v1/groups/{web['id']}/members", headers=auth_h(admin_token))
    assert r.json() == [host_id]
    r = await client.get(f"/api/v1/hosts/{host_id}/groups", headers=auth_h(admin_token))
    assert [g["name"] for g in r.json()] == ["web"]

    # Parameters at every level of the chain + host override.
    for target, value in [
        (f"/api/v1/groups/{gid}/parameters/ntp", "pool.ntp.org"),
        (f"/api/v1/groups/{east['id']}/parameters/ntp", "ntp.east.internal"),
        (f"/api/v1/groups/{east['id']}/parameters/syslog", "syslog.east"),
        (f"/api/v1/groups/{web['id']}/parameters/role", "web"),
    ]:
        r = await client.put(target, headers=auth_h(admin_token), json={"value": value})
        assert r.status_code == 200, r.text

    r = await client.put(
        f"/api/v1/hosts/{host_id}/parameters/role",
        headers=auth_h(admin_token),
        json={"value": "canary"},
    )
    assert r.status_code == 200

    r = await client.get(
        f"/api/v1/hosts/{host_id}/effective-parameters", headers=auth_h(admin_token)
    )
    assert r.status_code == 200
    effective = {p["key"]: p for p in r.json()}
    assert effective["ntp"]["value"] == "ntp.east.internal"
    assert effective["ntp"]["source_name"] == "dc-east"
    assert effective["syslog"]["value"] == "syslog.east"
    assert effective["role"]["value"] == "canary"
    assert effective["role"]["source_kind"] == "host"

    # Unset the host override: the group value shows through again.
    r = await client.delete(f"/api/v1/hosts/{host_id}/parameters/role", headers=auth_h(admin_token))
    assert r.status_code == 204
    r = await client.get(
        f"/api/v1/hosts/{host_id}/effective-parameters", headers=auth_h(admin_token)
    )
    effective = {p["key"]: p for p in r.json()}
    assert effective["role"]["value"] == "web"
    assert effective["role"]["source_name"] == "web"


async def test_invalid_parameter_key_rejected(client: AsyncClient, admin_token: str) -> None:
    gid = await _global_id(client, admin_token)
    r = await client.put(
        f"/api/v1/groups/{gid}/parameters/Bad-Key",
        headers=auth_h(admin_token),
        json={"value": 1},
    )
    assert r.status_code == 422


async def test_group_audit_trail(client: AsyncClient, admin_token: str) -> None:
    east = await _mk_group(client, admin_token, "audit-grp")
    host_id = await _mk_host(client, admin_token, "audit-node")
    await client.post(
        f"/api/v1/groups/{east['id']}/members",
        headers=auth_h(admin_token),
        json={"host_id": host_id},
    )
    await client.put(
        f"/api/v1/groups/{east['id']}/parameters/tz",
        headers=auth_h(admin_token),
        json={"value": "UTC"},
    )
    audit = await client.get("/api/v1/audit", headers=auth_h(admin_token))
    actions = {e["action"] for e in audit.json()["items"]}
    assert {"group.create", "group.add_member", "group.set_parameter"} <= actions
