from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from tests.conftest import auth_h


async def test_login_wrong_password_returns_401(client: AsyncClient, admin_user: Any) -> None:
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": "nope"},
    )
    assert r.status_code == 401


async def test_me_returns_current_user(client: AsyncClient, admin_token: str) -> None:
    r = await client.get("/api/v1/auth/me", headers=auth_h(admin_token))
    assert r.status_code == 200
    assert r.json()["email"] == "admin@example.com"
    assert r.json()["is_admin"] is True


async def test_admin_can_create_user(client: AsyncClient, admin_token: str) -> None:
    r = await client.post(
        "/api/v1/users",
        headers=auth_h(admin_token),
        json={
            "email": "new@example.com",
            "display_name": "New",
            "password": "abcdef1234",
            "is_admin": False,
        },
    )
    assert r.status_code == 201
    assert r.json()["email"] == "new@example.com"


async def test_non_admin_cannot_create_user(client: AsyncClient, user_token: str) -> None:
    r = await client.post(
        "/api/v1/users",
        headers=auth_h(user_token),
        json={
            "email": "other@example.com",
            "display_name": "Other",
            "password": "abcdef1234",
        },
    )
    assert r.status_code == 403


async def test_duplicate_email_conflicts(client: AsyncClient, admin_token: str) -> None:
    payload = {"email": "dupe@example.com", "display_name": "Dup", "password": "abcdef1234"}
    r1 = await client.post("/api/v1/users", headers=auth_h(admin_token), json=payload)
    assert r1.status_code == 201
    r2 = await client.post("/api/v1/users", headers=auth_h(admin_token), json=payload)
    assert r2.status_code == 409


async def test_logout_invalidates_token(client: AsyncClient, user_token: str) -> None:
    r = await client.post("/api/v1/auth/logout", headers=auth_h(user_token))
    assert r.status_code == 204
    r2 = await client.get("/api/v1/auth/me", headers=auth_h(user_token))
    assert r2.status_code == 401


@pytest.mark.parametrize("path", ["/api/v1/users", "/api/v1/teams", "/api/v1/hosts"])
async def test_unauthenticated_endpoints_require_token(client: AsyncClient, path: str) -> None:
    r = await client.get(path)
    assert r.status_code == 401
