"""UI integration tests: host search/filtering, groups pages, enrollment wizard."""

from __future__ import annotations

import re
from typing import Any

from httpx import AsyncClient

from tests.conftest import auth_h
from tests.integration.test_ui import _ui_login


async def _mk_host(client: AsyncClient, token: str, hostname: str) -> str:
    body: dict[str, Any] = {"hostname": hostname, "status": "active"}
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
    a = await _mk_host(client, admin_token, "alpha.example.com")
    b = await _mk_host(client, admin_token, "beta.example.com")

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
    host_id = await _mk_host(client, admin_token, "grp-ui.example.com")
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
            "hostname": "wizard-node",
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


# --- Host pane restructure ---------------------------------------------------


async def test_advanced_pane_is_present_but_closed(client: AsyncClient, admin_token: str) -> None:
    """Identifiers and timing are reachable without being in the way."""
    host_id = await _mk_host(client, admin_token, "adv-node")
    await _ui_login(client, "admin@example.com", "admin-pw-1234")

    r = await client.get(f"/hosts/{host_id}")
    assert "Advanced" in r.text
    assert "card-collapsible" in r.text
    # A <details> with no `open` attribute; the server renders it, the browser
    # keeps it folded.
    assert '<details class="card card-collapsible">' in r.text
    # Moved out of the Host card, so it is only in the collapsed pane.
    assert r.text.index("Advanced") < r.text.index("Last heartbeat")


async def test_facts_have_their_own_pane(client: AsyncClient, admin_token: str) -> None:
    """Facts left Overview: the fact table must not be on both."""
    host_id = await _mk_host(client, admin_token, "facts-node")
    agent = await _agent_for(client, admin_token, host_id)
    await client.post(
        "/api/v1/agents/inventory",
        headers=auth_h(agent),
        json={"facts": {"kernel": "6.9.3", "hostname": "facts-node"}, "components": []},
    )
    await _ui_login(client, "admin@example.com", "admin-pw-1234")

    overview = await client.get(f"/hosts/{host_id}")
    assert "All facts" not in overview.text

    facts = await client.get(f"/hosts/{host_id}", params={"tab": "facts"})
    assert "All facts" in facts.text
    assert "6.9.3" in facts.text


async def test_supportive_data_rides_in_a_sub_row(client: AsyncClient, admin_token: str) -> None:
    """Opening a card's disclosure adds a row, it does not add columns.

    Extra columns reflowed the table under the reader; the detail now hangs off
    each row as its own dimmed sub-row, so the primary columns never move.
    """
    host_id = await _mk_host(client, admin_token, "subrow-node")
    agent = await _agent_for(client, admin_token, host_id)
    ing = await client.post(
        "/api/v1/agents/inventory",
        headers=auth_h(agent),
        json={
            "facts": {"kernel": "6.9.3", "hostname": "subrow-node"},
            "components": [
                {
                    "kind": "disk",
                    "slot": "/dev/sda",
                    "serial": "SN-SUBROW-1",
                    "attrs": {"kind": "disk", "size_bytes": 512110190592, "bus": "sata"},
                },
                {
                    "kind": "nic",
                    "slot": "eth0",
                    "attrs": {"kind": "nic", "mac": "aa:bb:cc:dd:ee:ff", "speed_mbps": 1000},
                },
                {
                    "kind": "cpu",
                    "model": "Test CPU",
                    "attrs": {"kind": "cpu", "cores": 8, "threads": 16, "base_hz": 3400000000},
                },
                {
                    "kind": "memory",
                    "slot": "DIMM0",
                    "attrs": {"kind": "memory", "size_bytes": 17179869184, "speed_mts": 3200},
                },
                {
                    "kind": "gpu",
                    "model": "Test GPU",
                    "attrs": {"kind": "gpu", "vram_bytes": 8589934592},
                },
            ],
        },
    )
    assert ing.status_code == 200, ing.text
    await _ui_login(client, "admin@example.com", "admin-pw-1234")

    for tab in ("overview", "facts", "storage", "network", "cpu", "memory", "gpu", "groups"):
        r = await client.get(f"/hosts/{host_id}", params={"tab": tab})
        assert r.status_code == 200, tab
        # The old mechanism is gone everywhere, not just where it was noticed.
        assert "adv-col" not in r.text, tab
        # Groups has no parameters on a bare host, so it has no rows to hang a
        # sub-row off; every other table pane does.
        if tab not in ("overview", "groups"):
            assert 'class="adv-sub"' in r.text, tab

    storage = await client.get(f"/hosts/{host_id}", params={"tab": "storage"})
    serial_at = storage.text.index("SN-SUBROW-1")
    # The serial is inside the sub-row that follows its disk, not in a cell of
    # the disk's own row.
    assert storage.text.rindex('class="adv-sub"', 0, serial_at) > storage.text.rindex(
        "/dev/sda", 0, serial_at
    )


async def test_audit_payload_rides_in_a_sub_row(client: AsyncClient, admin_token: str) -> None:
    await _mk_host(client, admin_token, "audit-subrow-node")
    await _ui_login(client, "admin@example.com", "admin-pw-1234")

    r = await client.get("/audit")
    assert r.status_code == 200
    assert 'class="adv-sub"' in r.text
    assert "sub-json" in r.text
    assert "adv-col" not in r.text


async def test_description_is_editable(client: AsyncClient, admin_token: str) -> None:
    host_id = await _mk_host(client, admin_token, "desc-node")
    await _ui_login(client, "admin@example.com", "admin-pw-1234")
    csrf = client.cookies["sum_csrf"]

    r = await client.post(
        f"/hosts/{host_id}/description",
        data={"csrf_token": csrf, "description": "rack 4, top shelf"},
    )
    assert r.status_code == 303, r.text

    detail = await client.get(f"/hosts/{host_id}")
    assert "rack 4, top shelf" in detail.text

    # The service layer writes the audit entry; the UI route must not bypass it.
    audit = await client.get(
        "/api/v1/audit", headers=auth_h(admin_token), params={"action": "host.update"}
    )
    assert any(e["target_id"] == host_id for e in audit.json()["items"])


async def test_description_clears_to_empty(client: AsyncClient, admin_token: str) -> None:
    host_id = await _mk_host(client, admin_token, "clear-node")
    await _ui_login(client, "admin@example.com", "admin-pw-1234")
    csrf = client.cookies["sum_csrf"]

    await client.post(
        f"/hosts/{host_id}/description", data={"csrf_token": csrf, "description": "temporary"}
    )
    r = await client.post(
        f"/hosts/{host_id}/description", data={"csrf_token": csrf, "description": ""}
    )
    assert r.status_code == 303

    got = await client.get(f"/api/v1/hosts/{host_id}", headers=auth_h(admin_token))
    assert got.json()["description"] is None


async def test_description_edit_needs_ownership(
    client: AsyncClient, admin_token: str, regular_user: Any
) -> None:
    """Same gate as PATCH /api/v1/hosts/{id}: owner or admin, not any logged-in user."""
    host_id = await _mk_host(client, admin_token, "not-yours")
    await _ui_login(client, "user@example.com", "user-pw-1234")
    csrf = client.cookies["sum_csrf"]

    r = await client.post(
        f"/hosts/{host_id}/description", data={"csrf_token": csrf, "description": "mine now"}
    )
    assert r.status_code == 403


async def test_enrollment_wizard_takes_a_hostname(client: AsyncClient, admin_token: str) -> None:
    await _ui_login(client, "admin@example.com", "admin-pw-1234")
    csrf = client.cookies["sum_csrf"]

    r = await client.post(
        "/hosts/enroll",
        data={"csrf_token": csrf, "hostname": "seeded-name", "ttl_seconds": "3600"},
    )
    assert r.status_code == 200, r.text
    assert "seeded-name" in r.text


async def test_enrollment_wizard_generates_a_placeholder_hostname(
    client: AsyncClient, admin_token: str
) -> None:
    """The agent overwrites it on first inventory, so an empty field is fine."""
    await _ui_login(client, "admin@example.com", "admin-pw-1234")
    csrf = client.cookies["sum_csrf"]

    r = await client.post("/hosts/enroll", data={"csrf_token": csrf, "ttl_seconds": "3600"})
    assert r.status_code == 200, r.text
    assert re.search(r"host-\d{8}-\d{6}", r.text)
