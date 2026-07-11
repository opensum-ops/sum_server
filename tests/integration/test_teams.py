from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import auth_h


async def _create_team(client: AsyncClient, token: str, name: str = "platform") -> dict:
    r = await client.post(
        "/api/v1/teams",
        headers=auth_h(token),
        json={"name": name, "description": "Owns prod"},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_admin_creates_and_lists_team(client: AsyncClient, admin_token: str) -> None:
    team = await _create_team(client, admin_token)
    r = await client.get("/api/v1/teams", headers=auth_h(admin_token))
    assert r.status_code == 200
    names = [t["name"] for t in r.json()["items"]]
    assert team["name"] in names


async def test_duplicate_team_name_conflicts(client: AsyncClient, admin_token: str) -> None:
    await _create_team(client, admin_token, "alpha")
    r = await client.post("/api/v1/teams", headers=auth_h(admin_token), json={"name": "alpha"})
    assert r.status_code == 409


async def test_member_lifecycle(
    client: AsyncClient,
    admin_token: str,
    regular_user,
) -> None:
    team = await _create_team(client, admin_token, "ops")
    add = await client.post(
        f"/api/v1/teams/{team['id']}/members",
        headers=auth_h(admin_token),
        json={"user_id": str(regular_user.id), "role": "admin"},
    )
    assert add.status_code == 201
    # Admin can demote ... but should fail since they'd be the only admin.
    demote = await client.patch(
        f"/api/v1/teams/{team['id']}/members/{regular_user.id}",
        headers=auth_h(admin_token),
        json={"role": "member"},
    )
    assert demote.status_code == 409
