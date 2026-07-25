"""UI live-search tests: the rows partial and the suggestion menu.

The search bar is only an editor over the existing query params, so these
exercise the two endpoints it drives plus the visibility scoping that keeps a
non-admin from enumerating values off hosts they cannot read.
"""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient

from tests.conftest import auth_h
from tests.integration.test_ui import _ui_login


async def _host_with_facts(
    client: AsyncClient, token: str, name: str, hostname: str, facts: dict[str, Any]
) -> str:
    r = await client.post(
        "/api/v1/hosts", headers=auth_h(token), json={"name": name, "status": "active"}
    )
    assert r.status_code == 201, r.text
    host_id: str = r.json()["id"]

    er = await client.post(
        "/api/v1/agents/enrollments", headers=auth_h(token), json={"host_id": host_id}
    )
    en = await client.post(
        "/api/v1/agents/enroll", json={"enrollment_token": er.json()["enrollment_token"]}
    )
    agent_token = en.json()["agent_token"]
    inv = await client.post(
        "/api/v1/agents/inventory",
        headers=auth_h(agent_token),
        json={"facts": {"hostname": hostname, **facts}, "components": []},
    )
    assert inv.status_code == 200, inv.text
    return host_id


async def test_rows_partial_is_filtered_and_shell_free(
    client: AsyncClient, admin_token: str
) -> None:
    await _host_with_facts(
        client, admin_token, "rows-a", "rows-a.example.com", {"kernel": "6.9.3-x64v3"}
    )
    await _host_with_facts(
        client, admin_token, "rows-b", "rows-b.example.com", {"kernel": "5.10.0-legacy"}
    )
    await _ui_login(client, "admin@example.com", "admin-pw-1234")

    r = await client.get("/hosts/rows", params={"fact": "kernel:6.9.3-x64v3"})
    assert r.status_code == 200
    assert "rows-a.example.com" in r.text
    assert "rows-b.example.com" not in r.text
    # A partial, not a page: no shell, no nav.
    assert "<html" not in r.text
    assert "Sign out" not in r.text
    # Filtered keys still render as columns, same as the full page.
    assert "fact: kernel" in r.text


async def test_rows_partial_matches_full_page(client: AsyncClient, admin_token: str) -> None:
    """The live swap and a fresh page load must agree, or a shared URL lies."""
    await _host_with_facts(
        client, admin_token, "parity", "parity.example.com", {"kernel": "6.9.3-x64v3"}
    )
    await _ui_login(client, "admin@example.com", "admin-pw-1234")

    page = await client.get("/hosts", params={"fact": "kernel:6.9.3-x64v3"})
    rows = await client.get("/hosts/rows", params={"fact": "kernel:6.9.3-x64v3"})
    assert "parity.example.com" in page.text
    assert "parity.example.com" in rows.text
    # The page embeds the same partial, so the row markup appears verbatim.
    assert rows.text.strip() in page.text


async def test_search_bar_seeds_tokens_from_url(client: AsyncClient, admin_token: str) -> None:
    await _ui_login(client, "admin@example.com", "admin-pw-1234")
    r = await client.get("/hosts", params={"presence": "online", "fact": "kernel:6.9"})
    assert 'data-tokens="presence:online fact:kernel=6.9"' in r.text


async def test_suggest_fields_then_values(client: AsyncClient, admin_token: str) -> None:
    await _host_with_facts(client, admin_token, "sug", "sug.example.com", {"kernel": "6.9.3-x64v3"})
    await _ui_login(client, "admin@example.com", "admin-pw-1234")

    # No colon yet: the field vocabulary.
    r = await client.get("/hosts/suggest", params={"token": ""})
    assert "presence:" in r.text
    assert "group:" in r.text
    assert "fact:" in r.text

    # Prefix narrows it.
    r = await client.get("/hosts/suggest", params={"token": "pres"})
    assert "presence:" in r.text
    assert "group:" not in r.text

    # Field chosen: its values.
    r = await client.get("/hosts/suggest", params={"token": "presence:"})
    assert "online" in r.text
    assert "unreachable" in r.text

    # Fact keys come from what agents actually reported.
    r = await client.get("/hosts/suggest", params={"token": "fact:"})
    assert "kernel" in r.text
    assert 'data-value="fact:kernel="' in r.text

    # Then that key's observed values.
    r = await client.get("/hosts/suggest", params={"token": "fact:kernel="})
    assert "6.9.3-x64v3" in r.text


async def test_suggest_group_names(client: AsyncClient, admin_token: str) -> None:
    await client.post(
        "/api/v1/groups",
        headers=auth_h(admin_token),
        json={"name": "suggest-east", "description": "east dc"},
    )
    await _ui_login(client, "admin@example.com", "admin-pw-1234")

    r = await client.get("/hosts/suggest", params={"token": "group:sug"})
    assert "suggest-east" in r.text
    assert 'data-value="group:suggest-east"' in r.text


async def test_suggestions_are_visibility_scoped(
    client: AsyncClient, admin_token: str, regular_user: Any
) -> None:
    """A non-admin must not learn fact keys from hosts they cannot read."""
    await _host_with_facts(
        client,
        admin_token,
        "secret-node",
        "secret.example.com",
        {"classified_key": "classified-value"},
    )

    # The admin, who can see every host, is offered the key.
    await _ui_login(client, "admin@example.com", "admin-pw-1234")
    r = await client.get("/hosts/suggest", params={"token": "fact:"})
    assert "classified_key" in r.text

    # The regular user owns nothing, so the key must not leak.
    await _ui_login(client, "user@example.com", "user-pw-1234")
    r = await client.get("/hosts/suggest", params={"token": "fact:"})
    assert r.status_code == 200
    assert "classified_key" not in r.text

    r = await client.get("/hosts/suggest", params={"token": "fact:classified_key="})
    assert "classified-value" not in r.text


async def test_rows_partial_requires_login(client: AsyncClient) -> None:
    r = await client.get("/hosts/rows", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")
