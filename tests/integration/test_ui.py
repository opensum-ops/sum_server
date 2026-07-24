from __future__ import annotations

from typing import Any

from httpx import AsyncClient

from tests.conftest import auth_h


async def _ui_login(client: AsyncClient, email: str, password: str) -> str:
    """Log in through the UI form; returns the CSRF token for later POSTs."""
    r = await client.get("/login")
    assert r.status_code == 200
    csrf = client.cookies["sum_csrf"]
    r = await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf},
    )
    assert r.status_code == 303, r.text
    return csrf


async def test_page_requires_login(client: AsyncClient) -> None:
    r = await client.get("/hosts")
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=/hosts"


async def test_login_page_sets_csrf_cookie(client: AsyncClient) -> None:
    r = await client.get("/login")
    assert r.status_code == 200
    assert "sum_csrf" in r.cookies


async def test_login_rejects_forged_csrf(client: AsyncClient, admin_user: Any) -> None:
    await client.get("/login")
    r = await client.post(
        "/login",
        data={
            "email": "admin@example.com",
            "password": "admin-pw-1234",
            "csrf_token": "forged",
        },
    )
    assert r.status_code == 403


async def test_login_bad_password_shows_error(client: AsyncClient, admin_user: Any) -> None:
    await client.get("/login")
    csrf = client.cookies["sum_csrf"]
    r = await client.post(
        "/login",
        data={"email": "admin@example.com", "password": "wrong", "csrf_token": csrf},
    )
    assert r.status_code == 401
    assert "Invalid email or password" in r.text


async def test_login_open_redirect_guarded(client: AsyncClient, admin_user: Any) -> None:
    await client.get("/login")
    csrf = client.cookies["sum_csrf"]
    r = await client.post(
        "/login",
        data={
            "email": "admin@example.com",
            "password": "admin-pw-1234",
            "csrf_token": csrf,
            "next": "//evil.example.com/phish",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/hosts"


async def test_login_logout_flow(client: AsyncClient, admin_user: Any) -> None:
    csrf = await _ui_login(client, "admin@example.com", "admin-pw-1234")
    r = await client.get("/hosts")
    assert r.status_code == 200
    assert "Hosts" in r.text

    r = await client.post("/logout", data={"csrf_token": csrf})
    assert r.status_code == 303
    r = await client.get("/hosts")
    assert r.status_code == 303  # session revoked server-side


async def test_server_list_and_detail_render(client: AsyncClient, admin_token: str) -> None:
    cr = await client.post(
        "/api/v1/hosts",
        headers=auth_h(admin_token),
        json={"name": "ui-node", "hostname": "ui-node.example.com", "status": "active"},
    )
    assert cr.status_code == 201, cr.text
    host_id = cr.json()["id"]

    await _ui_login(client, "admin@example.com", "admin-pw-1234")
    r = await client.get("/hosts")
    assert r.status_code == 200
    assert "ui-node" in r.text

    r = await client.get(f"/hosts/{host_id}")
    assert r.status_code == 200
    assert "ui-node.example.com" in r.text
    assert "Summary" in r.text  # overview tab is the default
    assert "Facts" in r.text

    # Tabs render their sections.
    r = await client.get(f"/hosts/{host_id}", params={"tab": "storage"})
    assert "Disks" in r.text
    r = await client.get(f"/hosts/{host_id}", params={"tab": "network"})
    assert "Interfaces" in r.text
    r = await client.get(f"/hosts/{host_id}", params={"tab": "hardware"})
    assert "CPUs" in r.text
    r = await client.get(f"/hosts/{host_id}", params={"tab": "groups"})
    assert "Effective parameters" in r.text


async def test_server_detail_hidden_from_non_owner(
    client: AsyncClient, admin_token: str, regular_user: Any
) -> None:
    cr = await client.post(
        "/api/v1/hosts",
        headers=auth_h(admin_token),
        json={"name": "hidden-node", "status": "active"},
    )
    assert cr.status_code == 201
    host_id = cr.json()["id"]

    await _ui_login(client, "user@example.com", "user-pw-1234")
    r = await client.get(f"/hosts/{host_id}")
    assert r.status_code == 403


async def test_audit_page_admin_only(
    client: AsyncClient, admin_user: Any, regular_user: Any
) -> None:
    await _ui_login(client, "user@example.com", "user-pw-1234")
    r = await client.get("/audit")
    assert r.status_code == 403

    csrf = client.cookies["sum_csrf"]
    r = await client.post("/logout", data={"csrf_token": csrf})
    assert r.status_code == 303

    await _ui_login(client, "admin@example.com", "admin-pw-1234")
    r = await client.get("/audit")
    assert r.status_code == 200
    assert "Audit log" in r.text


async def test_settings_page_admin_only(
    client: AsyncClient, admin_user: Any, regular_user: Any
) -> None:
    await _ui_login(client, "user@example.com", "user-pw-1234")
    r = await client.get("/settings")
    assert r.status_code == 403

    csrf = client.cookies["sum_csrf"]
    r = await client.post("/logout", data={"csrf_token": csrf})
    assert r.status_code == 303

    await _ui_login(client, "admin@example.com", "admin-pw-1234")
    r = await client.get("/settings")
    assert r.status_code == 200
    assert "Settings" in r.text
    assert "sum_server" in r.text  # updates panel
    assert "System" in r.text
