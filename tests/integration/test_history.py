"""Change history: what gets recorded on ingest and edit, and how it reads back.

The recording tests drive the real ingest and edit paths rather than calling
``history.record`` directly, because the thing worth asserting is that the old
value is captured *before* the overwrite that destroys it.
"""

from __future__ import annotations

import uuid
from typing import Any

from httpx import AsyncClient

from tests.conftest import auth_h
from tests.integration.test_ui import _ui_login


async def _mk_host(client: AsyncClient, token: str, hostname: str) -> str:
    r = await client.post(
        "/api/v1/hosts", headers=auth_h(token), json={"hostname": hostname, "status": "active"}
    )
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
    assert en.status_code == 200, en.text
    agent_token: str = en.json()["agent_token"]
    return agent_token


def _disk(serial: str = "D-1", size: int = 1000, **attrs: Any) -> dict[str, Any]:
    return {
        "kind": "disk",
        "vendor": "Samsung",
        "model": "PM9A3",
        "serial": serial,
        "slot": "nvme0",
        "attrs": {"kind": "disk", "size_bytes": size, "bus": "nvme", **attrs},
    }


async def _ingest(client: AsyncClient, agent: str, **payload: Any) -> None:
    r = await client.post("/api/v1/agents/inventory", headers=auth_h(agent), json=payload)
    assert r.status_code == 200, r.text


async def _rows(db_session: Any, host_id: str, **filters: Any) -> list[Any]:
    """Read history straight from the table, newest first."""
    from sqlalchemy import select

    from sum_server.history.models import HostChange

    stmt = select(HostChange).where(HostChange.host_id == uuid.UUID(host_id))
    for column, value in filters.items():
        stmt = stmt.where(getattr(HostChange, column) == value)
    stmt = stmt.order_by(HostChange.observed_at.desc(), HostChange.id.desc())
    return list((await db_session.execute(stmt)).scalars().all())


# --- Recording: facts -------------------------------------------------------


async def test_fact_lifecycle_is_recorded(
    client: AsyncClient, admin_token: str, db_session: Any
) -> None:
    """A fact appearing, changing, and disappearing across three snapshots."""
    host_id = await _mk_host(client, admin_token, "h1.example.com")
    agent = await _agent_for(client, admin_token, host_id)

    await _ingest(client, agent, facts={"kernel": "6.9.3", "arch": "x86_64"}, components=[])
    await _ingest(client, agent, facts={"kernel": "6.10.0", "arch": "x86_64"}, components=[])
    await _ingest(client, agent, facts={"arch": "x86_64"}, components=[])

    kernel = await _rows(db_session, host_id, scope="fact", field="kernel")
    assert [(r.change, r.old_value, r.new_value) for r in kernel] == [
        ("del", "6.10.0", None),
        ("edit", "6.9.3", "6.10.0"),
        ("add", None, "6.9.3"),
    ]

    # arch never moved, so it has exactly its one `add` and nothing since.
    arch = await _rows(db_session, host_id, scope="fact", field="arch")
    assert [r.change for r in arch] == ["add"]


async def test_unchanged_snapshot_records_nothing(
    client: AsyncClient, admin_token: str, db_session: Any
) -> None:
    """The common case. An agent re-reporting identical data must be silent."""
    host_id = await _mk_host(client, admin_token, "h2.example.com")
    agent = await _agent_for(client, admin_token, host_id)

    payload: dict[str, Any] = {"facts": {"kernel": "6.9.3"}, "components": [_disk()]}
    await _ingest(client, agent, **payload)
    before = len(await _rows(db_session, host_id))
    await _ingest(client, agent, **payload)
    await _ingest(client, agent, **payload)

    assert len(await _rows(db_session, host_id)) == before


async def test_hostname_adoption_is_recorded_as_a_host_change(
    client: AsyncClient, admin_token: str, db_session: Any
) -> None:
    """The operator's enrollment label being replaced by the observed hostname."""
    host_id = await _mk_host(client, admin_token, "provisional-label")
    agent = await _agent_for(client, admin_token, host_id)

    await _ingest(client, agent, facts={"hostname": "real.example.com"}, components=[])

    rows = await _rows(db_session, host_id, scope="host", field="hostname")
    assert [(r.change, r.old_value, r.new_value) for r in rows] == [
        ("edit", "provisional-label", "real.example.com")
    ]
    assert rows[0].actor_kind == "agent"


# --- Recording: components --------------------------------------------------


async def test_component_attr_change_is_recorded_per_key(
    client: AsyncClient, admin_token: str, db_session: Any
) -> None:
    host_id = await _mk_host(client, admin_token, "h3.example.com")
    agent = await _agent_for(client, admin_token, host_id)

    await _ingest(client, agent, facts={}, components=[_disk(size=1000)])
    await _ingest(client, agent, facts={}, components=[_disk(size=2000)])

    rows = await _rows(db_session, host_id, scope="component")
    assert [(r.field, r.change, r.old_value, r.new_value) for r in rows] == [
        ("attrs.size_bytes", "edit", 1000, 2000),
        (
            "component",
            "add",
            None,
            {"vendor": "Samsung", "model": "PM9A3", "serial": "D-1", "slot": "nvme0"},
        ),
    ]
    # The label is snapshotted so the timeline survives the row being replaced.
    assert rows[0].subject_label == "nvme0"
    assert rows[0].component_kind == "disk"


async def test_component_disappearing_is_recorded_once(
    client: AsyncClient, admin_token: str, db_session: Any
) -> None:
    """Absence is a change; staying absent is not, or every ingest would write."""
    host_id = await _mk_host(client, admin_token, "h4.example.com")
    agent = await _agent_for(client, admin_token, host_id)

    await _ingest(client, agent, facts={}, components=[_disk()])
    await _ingest(client, agent, facts={}, components=[])
    await _ingest(client, agent, facts={}, components=[])

    present = await _rows(db_session, host_id, scope="component", field="present")
    assert [(r.change, r.old_value, r.new_value) for r in present] == [("edit", True, False)]


# --- Recording: human edits -------------------------------------------------


async def test_description_edit_through_the_ui_is_recorded(
    client: AsyncClient, admin_token: str, db_session: Any
) -> None:
    host_id = await _mk_host(client, admin_token, "h5.example.com")
    csrf = await _ui_login(client, "admin@example.com", "admin-pw-1234")

    await client.post(
        f"/hosts/{host_id}/description",
        data={"description": "rack 4", "csrf_token": csrf},
        follow_redirects=False,
    )
    await client.post(
        f"/hosts/{host_id}/description",
        data={"description": "rack 5", "csrf_token": csrf},
        follow_redirects=False,
    )

    rows = await _rows(db_session, host_id, scope="host", field="description")
    assert [(r.change, r.old_value, r.new_value) for r in rows] == [
        ("edit", "rack 4", "rack 5"),
        ("add", None, "rack 4"),
    ]
    # A human edit is attributed to the user, not to an agent.
    assert rows[0].actor_kind == "user"


async def test_group_membership_is_recorded_on_both_sides(
    client: AsyncClient, admin_token: str, db_session: Any
) -> None:
    host_id = await _mk_host(client, admin_token, "h6.example.com")
    gr = await client.post(
        "/api/v1/groups", headers=auth_h(admin_token), json={"name": "webservers"}
    )
    assert gr.status_code == 201, gr.text
    group_id = gr.json()["id"]

    add = await client.post(
        f"/api/v1/groups/{group_id}/members", headers=auth_h(admin_token), json={"host_id": host_id}
    )
    assert add.status_code in (200, 201, 204), add.text
    rm = await client.request(
        "DELETE", f"/api/v1/groups/{group_id}/members/{host_id}", headers=auth_h(admin_token)
    )
    assert rm.status_code in (200, 204), rm.text

    rows = await _rows(db_session, host_id, scope="group")
    assert [(r.change, r.subject_label) for r in rows] == [
        ("del", "webservers"),
        ("add", "webservers"),
    ]
    assert all(r.subject_id == uuid.UUID(group_id) for r in rows)


async def test_host_parameter_lifecycle_is_recorded(
    client: AsyncClient, admin_token: str, db_session: Any
) -> None:
    host_id = await _mk_host(client, admin_token, "h7.example.com")
    h = auth_h(admin_token)

    async def put(value: Any) -> None:
        r = await client.put(
            f"/api/v1/hosts/{host_id}/parameters/tier", headers=h, json={"value": value}
        )
        assert r.status_code in (200, 201), r.text

    await put("gold")
    await put("gold")  # Re-setting the same value is not a change.
    await put("silver")
    r = await client.delete(f"/api/v1/hosts/{host_id}/parameters/tier", headers=h)
    assert r.status_code in (200, 204), r.text

    rows = await _rows(db_session, host_id, scope="param", field="tier")
    assert [(r.change, r.old_value, r.new_value) for r in rows] == [
        ("del", "silver", None),
        ("edit", "gold", "silver"),
        ("add", None, "gold"),
    ]


async def test_reboot_is_recorded_but_heartbeats_are_not(
    client: AsyncClient, admin_token: str, db_session: Any
) -> None:
    """last_heartbeat_at moves every 30s; only a boot_id change is a change."""
    host_id = await _mk_host(client, admin_token, "h8.example.com")
    agent = await _agent_for(client, admin_token, host_id)

    for boot in ("boot-aaa", "boot-aaa", "boot-bbb", "boot-bbb"):
        r = await client.post(
            "/api/v1/agents/heartbeat", headers=auth_h(agent), json={"boot_id": boot}
        )
        assert r.status_code == 200, r.text

    rows = await _rows(db_session, host_id, scope="host", field="boot_id")
    assert [(r.change, r.old_value, r.new_value) for r in rows] == [
        ("edit", "boot-aaa", "boot-bbb"),
        ("add", None, "boot-aaa"),
    ]


# --- Reading: the fragment route --------------------------------------------


async def test_field_timeline_fragment(client: AsyncClient, admin_token: str) -> None:
    host_id = await _mk_host(client, admin_token, "h9.example.com")
    agent = await _agent_for(client, admin_token, host_id)
    await _ingest(client, agent, facts={"kernel": "6.9.3"}, components=[])
    await _ingest(client, agent, facts={"kernel": "6.10.0"}, components=[])

    await _ui_login(client, "admin@example.com", "admin-pw-1234")
    r = await client.get(f"/hosts/{host_id}/history", params={"scope": "fact", "field": "kernel"})
    assert r.status_code == 200
    assert "6.9.3" in r.text
    assert "6.10.0" in r.text
    # A single-field timeline does not repeat the field name it hangs off.
    assert "hist-field" not in r.text


async def test_pane_feed_spans_both_group_scopes(client: AsyncClient, admin_token: str) -> None:
    """The Groups pane covers memberships and host parameters in one feed."""
    host_id = await _mk_host(client, admin_token, "h10.example.com")
    gr = await client.post("/api/v1/groups", headers=auth_h(admin_token), json={"name": "db"})
    await client.post(
        f"/api/v1/groups/{gr.json()['id']}/members",
        headers=auth_h(admin_token),
        json={"host_id": host_id},
    )
    await client.put(
        f"/api/v1/hosts/{host_id}/parameters/tier",
        headers=auth_h(admin_token),
        json={"value": "gold"},
    )

    await _ui_login(client, "admin@example.com", "admin-pw-1234")
    r = await client.get(f"/hosts/{host_id}/history", params={"scope": "group,param"})
    assert r.status_code == 200
    assert "db" in r.text
    assert "tier" in r.text
    # A pane feed mixes fields, so each row is labeled.
    assert "hist-field" in r.text


async def test_unknown_scope_returns_nothing_rather_than_everything(
    client: AsyncClient, admin_token: str
) -> None:
    host_id = await _mk_host(client, admin_token, "h11.example.com")
    agent = await _agent_for(client, admin_token, host_id)
    await _ingest(client, agent, facts={"kernel": "6.9.3"}, components=[])

    await _ui_login(client, "admin@example.com", "admin-pw-1234")
    r = await client.get(f"/hosts/{host_id}/history", params={"scope": "everything"})
    assert r.status_code == 200
    assert "No changes recorded" in r.text
    assert "6.9.3" not in r.text


async def test_malformed_subject_returns_nothing(client: AsyncClient, admin_token: str) -> None:
    host_id = await _mk_host(client, admin_token, "h12.example.com")
    agent = await _agent_for(client, admin_token, host_id)
    await _ingest(client, agent, facts={"kernel": "6.9.3"}, components=[])

    await _ui_login(client, "admin@example.com", "admin-pw-1234")
    r = await client.get(f"/hosts/{host_id}/history", params={"subject_id": "not-a-uuid"})
    assert r.status_code == 200
    assert "No changes recorded" in r.text
    assert "6.9.3" not in r.text


async def test_history_is_gated_by_host_visibility(
    client: AsyncClient, admin_token: str, regular_user: Any
) -> None:
    """A change log leaks whatever the host page leaks, so it gates the same."""
    host_id = await _mk_host(client, admin_token, "secret.example.com")
    agent = await _agent_for(client, admin_token, host_id)
    await _ingest(client, agent, facts={"kernel": "classified-6.9.3"}, components=[])

    await _ui_login(client, "user@example.com", "user-pw-1234")
    r = await client.get(f"/hosts/{host_id}/history", params={"scope": "fact"})
    assert r.status_code == 403
    assert "classified-6.9.3" not in r.text


async def test_history_requires_login(client: AsyncClient, admin_token: str) -> None:
    host_id = await _mk_host(client, admin_token, "h13.example.com")
    r = await client.get(f"/hosts/{host_id}/history", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


# --- Reading: the host page -------------------------------------------------


async def test_host_page_renders_controls_with_counts(
    client: AsyncClient, admin_token: str
) -> None:
    host_id = await _mk_host(client, admin_token, "h14.example.com")
    agent = await _agent_for(client, admin_token, host_id)
    await _ingest(client, agent, facts={"kernel": "6.9.3"}, components=[_disk()])
    await _ingest(client, agent, facts={"kernel": "6.10.0"}, components=[_disk(size=2000)])

    await _ui_login(client, "admin@example.com", "admin-pw-1234")

    # Facts pane: a control per fact row, pointing at that field's timeline.
    r = await client.get(f"/hosts/{host_id}", params={"tab": "facts"})
    assert r.status_code == 200
    assert f"/hosts/{host_id}/history?scope=fact&amp;field=kernel" in r.text
    assert "Changes on this pane" in r.text

    # Storage pane: a control per component row, keyed by subject.
    r = await client.get(f"/hosts/{host_id}", params={"tab": "storage"})
    assert "scope=component&amp;subject_id=" in r.text
    assert "component_kind=disk" in r.text


async def test_every_pane_carries_its_own_feed(client: AsyncClient, admin_token: str) -> None:
    """Including groups, which is the one pane spanning two scopes."""
    host_id = await _mk_host(client, admin_token, "h15.example.com")
    await _ui_login(client, "admin@example.com", "admin-pw-1234")

    expected = {
        "overview": "scope=host",
        "facts": "scope=fact",
        "storage": "component_kind=disk",
        "network": "component_kind=nic",
        "cpu": "component_kind=cpu",
        "memory": "component_kind=memory",
        "gpu": "component_kind=gpu",
        "groups": "scope=group,param",
    }
    for tab, fragment in expected.items():
        r = await client.get(f"/hosts/{host_id}", params={"tab": tab})
        assert r.status_code == 200, tab
        assert "Changes on this pane" in r.text, tab
        assert fragment.replace("&", "&amp;") in r.text, tab
