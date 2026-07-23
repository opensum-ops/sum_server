from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import auth_h


async def _create_host(client: AsyncClient, token: str, name: str = "host-1") -> dict:
    r = await client.post(
        "/api/v1/hosts",
        headers=auth_h(token),
        json={"name": name, "hostname": f"{name}.example.com", "status": "active"},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_admin_can_create_and_read_server(client: AsyncClient, admin_token: str) -> None:
    host = await _create_host(client, admin_token)
    r = await client.get(f"/api/v1/hosts/{host['id']}", headers=auth_h(admin_token))
    assert r.status_code == 200
    assert r.json()["name"] == "host-1"


async def test_non_owner_cannot_read_server(
    client: AsyncClient,
    admin_token: str,
    user_token: str,
) -> None:
    host = await _create_host(client, admin_token, "secret")
    r = await client.get(f"/api/v1/hosts/{host['id']}", headers=auth_h(user_token))
    assert r.status_code == 403


async def test_user_owner_can_read_server(
    client: AsyncClient,
    admin_token: str,
    user_token: str,
    regular_user,
) -> None:
    host = await _create_host(client, admin_token, "shared")
    add = await client.post(
        f"/api/v1/hosts/{host['id']}/owners",
        headers=auth_h(admin_token),
        json={"user_id": str(regular_user.id)},
    )
    assert add.status_code == 204
    r = await client.get(f"/api/v1/hosts/{host['id']}", headers=auth_h(user_token))
    assert r.status_code == 200


async def test_decommission_freezes_ownership_changes(
    client: AsyncClient, admin_token: str, regular_user
) -> None:
    host = await _create_host(client, admin_token, "decom")
    d = await client.delete(f"/api/v1/hosts/{host['id']}", headers=auth_h(admin_token))
    assert d.status_code == 204
    add = await client.post(
        f"/api/v1/hosts/{host['id']}/owners",
        headers=auth_h(admin_token),
        json={"user_id": str(regular_user.id)},
    )
    assert add.status_code == 409
