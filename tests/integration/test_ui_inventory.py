"""UI integration tests: host search/filtering, groups pages, enrollment wizard."""

from __future__ import annotations

import re
from typing import Any

from httpx import AsyncClient

from tests.conftest import auth_h
from tests.integration.test_ui import _ui_login


async def _mk_host(client: AsyncClient, token: str, name: str, hostname: str | None = None) -> str:
    body: dict[str, Any] = {"name": name, "status": "active"}
    if hostname:
        body["hostname"] = hostname
    r = await client.post("/api/v1/hosts", headers=auth_h(token), json=body)
    assert r.status_code == 201, r.text
    host_id: str = r.json()["id"]
    return host_id


async def _agent_for(client: AsyncClient, token: str, host_id: str) -> str:
    er = await client.post(
        "/api/v1/agents/enrollments", headers=auth_h(token), json={"host_id": host_id}
    )
    en = await client.post(
        "/api/v1/agents/enroll", json={"enrollment_token": er.json()["enrollment_token"]}
    )
    assert en.status_code == 200
    agent_token: str = en.json()["agent_token"]
    return agent_token


async def test_host_search_filters(client: AsyncClient, admin_token: str) -> None:
    a = await _mk_host(client, admin_token, "search-a", "alpha.example.com")
    b = await _mk_host(client, admin_token, "search-b", "beta.example.com")

    # Give alpha a kernel fact + a disk; beta stays bare.
    agent = await _agent_for(client, admin_token, a)
    inv = await client.post(
        "/api/v1/agents/inventory",
        headers=auth_h(agent),
        json={
            "facts": {"kernel": "6.9.3-x64v3", "hostname": "alpha.example.com"},
            "components": [
                {
                    "kind": "disk",
                    "vendor": "Samsung",
                    "model": "PM9A3",
                    "serial": "UI-DISK-1",
                    "slot": "nvme0",
                    "attrs": {"kind": "disk", "size_bytes": 1000, "bus": "nvme"},
                }
            ],
        },
    )
    assert inv.status_code == 200, inv.text

    await _ui_login(client, "admin@example.com", "admin-pw-1234")

    # Free text matches hostname.
    r = await client.get("/hosts", params={"q": "alpha"})
    assert "alpha.example.com" in r.text
    assert "beta.example.com" not in r.text

    # Fact filter adds a dynamic column with the value.
    r = await client.get("/hosts", params={"fact": "kernel:6.9.3-x64v3"})
    assert "alpha.example.com" in r.text
    assert "beta.example.com" not in r.text
    assert "fact: kernel" in r.text
    assert "6.9.3-x64v3" in r.text

    # Component filter shows the matched component.
    r = await client.get("/hosts", params={"component": "PM9A3"})
    assert "alpha.example.com" in r.text
    assert "beta.example.com" not in r.text
    assert "disk: PM9A3" in r.text

    # Presence filter: neither host has heartbeated -> both pending.
    r = await client.get("/hosts", params={"presence": "online"})
    assert "alpha.example.com" not in r.text
    r = await client.get("/hosts", params={"presence": "pending"})
    assert "alpha.example.com" in r.text
    assert "beta.example.com" in r.text
    assert b  # silence unused warning


async def test_group_pages_and_membership_flow(client: AsyncClient, admin_token: str) -> None:
    host_id = await _mk_host(client, admin_token, "grp-ui-node", "grp-ui.example.com")
    await _ui_login(client, "admin@example.com", "admin-pw-1234")
    csrf = client.cookies["sum_csrf"]

    # Groups page shows the global root.
    r = await client.get("/groups")
    assert r.status_code == 200
    assert "global" in r.text

    # Create a group via the UI.
    gid_match = None
    r = await client.post(
        "/groups/create",
        data={
            "csrf_token": csrf,
            "name": "ui-east",
            "parent_id": _global_id(r.text),
            "description": "east dc",
        },
    )
    assert r.status_code == 303, r.text
    gid_match = r.headers["location"].rsplit("/", 1)[-1]

    # Group detail renders; set a parameter.
    r = await client.get(f"/groups/{gid_match}")
    assert "ui-east" in r.text
    r = await client.post(
        f"/groups/{gid_match}/params/set",
        data={"csrf_token": csrf, "key": "ntp", "value": '"ntp.east"'},
    )
    assert r.status_code == 303

    # Add the host as a member by hostname.
    r = await client.post(
        f"/groups/{gid_match}/members/add",
        data={"csrf_token": csrf, "identifier": "grp-ui.example.com"},
    )
    assert r.status_code == 303
    r = await client.get(f"/groups/{gid_match}")
    assert "grp-ui.example.com" in r.text

    # Host groups tab shows membership + inherited parameter with provenance.
    r = await client.get(f"/hosts/{host_id}", params={"tab": "groups"})
    assert "ui-east" in r.text
    assert "ntp" in r.text
    assert "ntp.east" in r.text

    # Group filter on the hosts list.
    r = await client.get("/hosts", params={"group": "ui-east"})
    assert "grp-ui.example.com" in r.text


def _global_id(groups_page_html: str) -> str:
    """Extract the global group's id from a rendered groups page."""
    m = re.search(r'href="/groups/([0-9a-f-]{36})">global</a>', groups_page_html)
    assert m, "global group link not found"
    return m.group(1)


async def test_enrollment_wizard_flow(client: AsyncClient, admin_user: Any) -> None:
    await _ui_login(client, "admin@example.com", "admin-pw-1234")
    csrf = client.cookies["sum_csrf"]

    r = await client.get("/hosts/enroll")
    assert r.status_code == 200
    assert "Enroll a new host" in r.text

    r = await client.post(
        "/hosts/enroll",
        data={
            "csrf_token": csrf,
            "name": "wizard-node",
            "description": "from the wizard",
            "ttl_seconds": "3600",
        },
    )
    assert r.status_code == 200, r.text
    assert "sum-agent enroll --token" in r.text
    assert "SUM_AGENT_SERVER_URL" in r.text
    assert "systemd" in r.text

    # The token in the page is a working enrollment token.
    m = re.search(r"sum-agent enroll --token ([^<\s]+)", r.text)
    assert m
    en = await client.post("/api/v1/agents/enroll", json={"enrollment_token": m.group(1)})
    assert en.status_code == 200

    # The host page for a pending host offers a new-token button.
    host_id = en.json()["host_id"]
    r = await client.get(f"/hosts/{host_id}")
    assert r.status_code == 200


async def test_enrollment_wizard_admin_only(client: AsyncClient, regular_user: Any) -> None:
    await _ui_login(client, "user@example.com", "user-pw-1234")
    r = await client.get("/hosts/enroll")
    assert r.status_code == 403


async def test_groups_page_visible_to_regular_user(client: AsyncClient, regular_user: Any) -> None:
    await _ui_login(client, "user@example.com", "user-pw-1234")
    r = await client.get("/groups")
    assert r.status_code == 200
    assert "Create group" not in r.text  # admin-only form hidden


async def test_group_update_form(client: AsyncClient, admin_token: str) -> None:
    """Rename/reparent through the UI form.

    Untested until 2026-07-26, and broken: the handler read the group, released
    the read transaction, then touched the now-expired instance inside its write
    transaction, which raises MissingGreenlet under asyncio.
    """
    g = await client.post(
        "/api/v1/groups",
        headers=auth_h(admin_token),
        json={"name": "rename-me", "description": "before"},
    )
    gid = g.json()["id"]
    await _ui_login(client, "admin@example.com", "admin-pw-1234")
    csrf = client.cookies["sum_csrf"]

    r = await client.post(
        f"/groups/{gid}/update",
        data={
            "csrf_token": csrf,
            "name": "renamed-group",
            "description": "after",
            "parent_id": "",
        },
    )
    assert r.status_code == 303, r.text
    detail = await client.get(f"/groups/{gid}")
    assert "renamed-group" in detail.text
    assert "after" in detail.text
