"""Agent removal: requesting it, delivering it, and what it clears.

Removal is desired state (hard constraint #1 puts the host out of reach), so
these drive the real request → heartbeat directive → goodbye → cleanup cycle
rather than calling the service directly. The parts worth asserting are the
ones that would quietly do the wrong thing: what survives cleanup, and which
host the delete button is even offered for.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy import func, select

from tests.conftest import auth_h
from tests.integration.test_ui import _ui_login


async def _mk_host(client: AsyncClient, token: str, hostname: str) -> str:
    r = await client.post(
        "/api/v1/hosts", headers=auth_h(token), json={"hostname": hostname, "status": "active"}
    )
    assert r.status_code == 201, r.text
    host_id: str = r.json()["id"]
    return host_id


async def _enroll(client: AsyncClient, token: str, host_id: str) -> str:
    er = await client.post(
        "/api/v1/agents/enrollments", headers=auth_h(token), json={"host_id": host_id}
    )
    en = await client.post(
        "/api/v1/agents/enroll", json={"enrollment_token": er.json()["enrollment_token"]}
    )
    assert en.status_code == 200, en.text
    agent_token: str = en.json()["agent_token"]
    return agent_token


async def _populate(client: AsyncClient, agent: str) -> None:
    """Give the host something for removal to clear."""
    r = await client.post(
        "/api/v1/agents/inventory",
        headers=auth_h(agent),
        json={
            "facts": {"kernel": "6.9.3", "hostname": "seeded.example.com"},
            "components": [
                {
                    "kind": "disk",
                    "vendor": "Samsung",
                    "model": "PM9A3",
                    "serial": "R-1",
                    "slot": "nvme0",
                    "attrs": {"kind": "disk", "size_bytes": 1000, "bus": "nvme"},
                }
            ],
        },
    )
    assert r.status_code == 200, r.text
    hb = await client.post(
        "/api/v1/agents/heartbeat", headers=auth_h(agent), json={"boot_id": "boot-1"}
    )
    assert hb.status_code == 200, hb.text


async def _count(db_session: Any, model: Any, **where: Any) -> int:
    stmt = select(func.count()).select_from(model)
    for col, val in where.items():
        stmt = stmt.where(getattr(model, col) == val)
    return int((await db_session.execute(stmt)).scalar_one())


# --- Requesting -------------------------------------------------------------


async def test_request_sets_pending_and_is_delivered_signed(
    client: AsyncClient, admin_token: str
) -> None:
    host_id = await _mk_host(client, admin_token, "r1.example.com")
    agent = await _enroll(client, admin_token, host_id)
    await _populate(client, agent)

    r = await client.post(f"/api/v1/hosts/{host_id}/agent-removal", headers=auth_h(admin_token))
    assert r.status_code == 204, r.text

    hb = await client.post("/api/v1/agents/heartbeat", headers=auth_h(agent), json={})
    assert hb.status_code == 200
    directive = hb.json()["agent_remove"]
    assert directive is not None
    assert directive["action"] == "remove_agent"

    # Signed against this host, so it cannot be replayed at another one.
    from sum_server.core.security import signing
    from sum_server.updates.directive import removal_signing_payload

    payload = removal_signing_payload(uuid.UUID(host_id), directive["requested_at"])
    assert signing.verify(payload, base64.b64decode(directive["signature"]))


async def test_requesting_twice_conflicts(client: AsyncClient, admin_token: str) -> None:
    host_id = await _mk_host(client, admin_token, "r2.example.com")
    await _enroll(client, admin_token, host_id)
    h = auth_h(admin_token)
    assert (
        await client.post(f"/api/v1/hosts/{host_id}/agent-removal", headers=h)
    ).status_code == 204
    assert (
        await client.post(f"/api/v1/hosts/{host_id}/agent-removal", headers=h)
    ).status_code == 409


async def test_removal_supersedes_a_pending_update(client: AsyncClient, admin_token: str) -> None:
    """An agent about to uninstall itself has no use for a new binary."""
    from sum_server.hosts.models import Host

    host_id = await _mk_host(client, admin_token, "r3.example.com")
    agent = await _enroll(client, admin_token, host_id)
    await _populate(client, agent)

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from sum_server.core.db import get_engine

    sm = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with sm() as s, s.begin():
        host = (await s.execute(select(Host).where(Host.id == uuid.UUID(host_id)))).scalar_one()
        host.target_agent_version = "9.9.9"

    r = await client.post(f"/api/v1/hosts/{host_id}/agent-removal", headers=auth_h(admin_token))
    assert r.status_code == 204

    hb = await client.post("/api/v1/agents/heartbeat", headers=auth_h(agent), json={})
    body = hb.json()
    assert body["agent_remove"] is not None
    assert body["agent_update"] is None


async def test_cancel_stops_the_directive(client: AsyncClient, admin_token: str) -> None:
    host_id = await _mk_host(client, admin_token, "r4.example.com")
    agent = await _enroll(client, admin_token, host_id)
    h = auth_h(admin_token)

    await client.post(f"/api/v1/hosts/{host_id}/agent-removal", headers=h)
    assert (
        await client.delete(f"/api/v1/hosts/{host_id}/agent-removal", headers=h)
    ).status_code == 204

    hb = await client.post("/api/v1/agents/heartbeat", headers=auth_h(agent), json={})
    assert hb.json()["agent_remove"] is None

    # Nothing pending any more, so a second cancel has nothing to do.
    assert (
        await client.delete(f"/api/v1/hosts/{host_id}/agent-removal", headers=h)
    ).status_code == 409


# --- Completing -------------------------------------------------------------


async def test_goodbye_clears_agent_derived_data_and_keeps_the_rest(
    client: AsyncClient, admin_token: str, db_session: Any
) -> None:
    from sum_server.auth.models import AgentToken
    from sum_server.components.models import Component
    from sum_server.history.models import HostChange
    from sum_server.hosts.models import Host, HostFact

    host_id = await _mk_host(client, admin_token, "r5.example.com")
    agent = await _enroll(client, admin_token, host_id)
    await _populate(client, agent)
    hid = uuid.UUID(host_id)

    # A human-owned value on the same host, which must survive.
    csrf = await _ui_login(client, "admin@example.com", "admin-pw-1234")
    await client.post(
        f"/hosts/{host_id}/description",
        data={"description": "rack 9", "csrf_token": csrf},
        follow_redirects=False,
    )
    await client.put(
        f"/api/v1/hosts/{host_id}/parameters/tier",
        headers=auth_h(admin_token),
        json={"value": "gold"},
    )

    assert await _count(db_session, HostFact, host_id=hid) > 0
    assert await _count(db_session, Component, host_id=hid) > 0

    await client.post(f"/api/v1/hosts/{host_id}/agent-removal", headers=auth_h(admin_token))
    gb = await client.post(
        "/api/v1/agents/heartbeat",
        headers=auth_h(agent),
        json={"state": "stopping", "detail": "agent_removed"},
    )
    assert gb.status_code == 200, gb.text

    # Agent-observed data is gone.
    assert await _count(db_session, HostFact, host_id=hid) == 0
    assert await _count(db_session, Component, host_id=hid) == 0

    # Every agent-written history row goes with it, except the tombstone the
    # removal itself writes: the page just lost all its facts and hardware, and
    # that row is what explains why.
    agent_rows = (
        (
            await db_session.execute(
                select(HostChange).where(
                    HostChange.host_id == hid, HostChange.actor_kind == "agent"
                )
            )
        )
        .scalars()
        .all()
    )
    assert [(r.scope, r.field, r.change) for r in agent_rows] == [("host", "agent", "del")]

    # The human's record of the same host is not.
    assert await _count(db_session, HostChange, host_id=hid, actor_kind="user") > 0

    host = (await db_session.execute(select(Host).where(Host.id == hid))).scalar_one()
    await db_session.refresh(host)
    assert host.description == "rack 9"
    assert host.last_heartbeat_at is None
    assert host.boot_id is None
    assert host.agent_removal_requested_at is None
    assert host.presence == "pending"

    # Credentials are revoked, not deleted: the revoked row is what remembers
    # this host was once real.
    tokens = (
        (await db_session.execute(select(AgentToken).where(AgentToken.host_id == hid)))
        .scalars()
        .all()
    )
    assert tokens
    assert all(t.revoked_at is not None for t in tokens)

    # And the revoked token is genuinely dead.
    after = await client.post("/api/v1/agents/heartbeat", headers=auth_h(agent), json={})
    assert after.status_code == 401


async def test_goodbye_is_honoured_without_a_request(
    client: AsyncClient, admin_token: str, db_session: Any
) -> None:
    """Covers a manual `uninstall.sh` on the box converging the server."""
    from sum_server.hosts.models import HostFact

    host_id = await _mk_host(client, admin_token, "r6.example.com")
    agent = await _enroll(client, admin_token, host_id)
    await _populate(client, agent)

    gb = await client.post(
        "/api/v1/agents/heartbeat",
        headers=auth_h(agent),
        json={"state": "stopping", "detail": "agent_removed"},
    )
    assert gb.status_code == 200
    assert await _count(db_session, HostFact, host_id=uuid.UUID(host_id)) == 0


async def test_ordinary_goodbye_is_untouched(client: AsyncClient, admin_token: str) -> None:
    """A reboot must not be mistaken for a removal."""
    host_id = await _mk_host(client, admin_token, "r7.example.com")
    agent = await _enroll(client, admin_token, host_id)
    await _populate(client, agent)

    gb = await client.post(
        "/api/v1/agents/heartbeat",
        headers=auth_h(agent),
        json={"state": "stopping", "detail": "rebooting"},
    )
    assert gb.status_code == 200
    r = await client.get(f"/api/v1/hosts/{host_id}", headers=auth_h(admin_token))
    assert r.json()["presence"] == "rebooting"


# --- Deleting a host that never had an agent --------------------------------


async def test_never_enrolled_host_is_deleted_outright(
    client: AsyncClient, admin_token: str, db_session: Any
) -> None:
    from sum_server.core.audit import AuditEntry
    from sum_server.hosts.models import Host

    host_id = await _mk_host(client, admin_token, "placeholder.example.com")
    hid = uuid.UUID(host_id)

    r = await client.delete(f"/api/v1/hosts/{host_id}/record", headers=auth_h(admin_token))
    assert r.status_code == 204, r.text
    assert await _count(db_session, Host, id=hid) == 0

    # The audit trail outlives the row: target_id is deliberately not a FK.
    entries = (
        (
            await db_session.execute(
                select(AuditEntry).where(
                    AuditEntry.target_id == hid, AuditEntry.action == "host.deleted"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(entries) == 1
    assert entries[0].payload["hostname"] == "placeholder.example.com"


async def test_enrolled_host_cannot_be_deleted_outright(
    client: AsyncClient, admin_token: str
) -> None:
    host_id = await _mk_host(client, admin_token, "real.example.com")
    await _enroll(client, admin_token, host_id)
    r = await client.delete(f"/api/v1/hosts/{host_id}/record", headers=auth_h(admin_token))
    assert r.status_code == 409, r.text


async def test_a_removed_host_still_cannot_be_deleted_outright(
    client: AsyncClient, admin_token: str
) -> None:
    """The bug that keying off presence would have caused.

    After removal the host reads `pending` again, exactly like one that was
    never enrolled. Pressing the destructive control a second time must not
    delete a real machine's record.
    """
    host_id = await _mk_host(client, admin_token, "r8.example.com")
    agent = await _enroll(client, admin_token, host_id)
    await _populate(client, agent)
    await client.post(
        "/api/v1/agents/heartbeat",
        headers=auth_h(agent),
        json={"state": "stopping", "detail": "agent_removed"},
    )

    got = await client.get(f"/api/v1/hosts/{host_id}", headers=auth_h(admin_token))
    assert got.json()["presence"] == "pending"

    r = await client.delete(f"/api/v1/hosts/{host_id}/record", headers=auth_h(admin_token))
    assert r.status_code == 409


async def test_removal_endpoints_are_admin_only(
    client: AsyncClient, admin_token: str, user_token: str
) -> None:
    host_id = await _mk_host(client, admin_token, "r9.example.com")
    h = auth_h(user_token)
    assert (
        await client.post(f"/api/v1/hosts/{host_id}/agent-removal", headers=h)
    ).status_code == 403
    assert (
        await client.delete(f"/api/v1/hosts/{host_id}/agent-removal", headers=h)
    ).status_code == 403
    assert (await client.delete(f"/api/v1/hosts/{host_id}/record", headers=h)).status_code == 403


# --- UI ---------------------------------------------------------------------


async def test_page_offers_the_control_that_matches_the_host(
    client: AsyncClient, admin_token: str
) -> None:
    """Two outcomes, two labels. One word covering both would hide a delete."""
    never = await _mk_host(client, admin_token, "never.example.com")
    real = await _mk_host(client, admin_token, "real2.example.com")
    agent = await _enroll(client, admin_token, real)
    await _populate(client, agent)

    await _ui_login(client, "admin@example.com", "admin-pw-1234")

    r = await client.get(f"/hosts/{never}")
    assert "Delete host" in r.text
    assert "Remove agent" not in r.text
    assert f"/hosts/{never}/delete" in r.text

    r = await client.get(f"/hosts/{real}")
    assert "Remove agent" in r.text
    assert "Delete host" not in r.text
    assert f"/hosts/{real}/agent-removal" in r.text


async def test_ui_removal_round_trip(client: AsyncClient, admin_token: str) -> None:
    host_id = await _mk_host(client, admin_token, "r10.example.com")
    agent = await _enroll(client, admin_token, host_id)
    await _populate(client, agent)
    csrf = await _ui_login(client, "admin@example.com", "admin-pw-1234")

    r = await client.post(
        f"/hosts/{host_id}/agent-removal",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303

    page = await client.get(f"/hosts/{host_id}")
    assert "Removal requested" in page.text
    assert "Cancel removal" in page.text
    # While removal is pending the destructive control is not offered again.
    assert "Remove agent" not in page.text

    r = await client.post(
        f"/hosts/{host_id}/agent-removal/cancel",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    page = await client.get(f"/hosts/{host_id}")
    assert "Cancel removal" not in page.text


async def test_ui_delete_redirects_to_the_list(client: AsyncClient, admin_token: str) -> None:
    host_id = await _mk_host(client, admin_token, "gone.example.com")
    csrf = await _ui_login(client, "admin@example.com", "admin-pw-1234")

    r = await client.post(
        f"/hosts/{host_id}/delete", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/hosts"
    assert (await client.get(f"/hosts/{host_id}")).status_code == 404


# --- uninstall.sh -----------------------------------------------------------


def test_uninstall_script_is_valid_shell() -> None:
    """A syntax error must never reach a root shell."""
    from sum_server.install.service import render_uninstall_script

    script = render_uninstall_script(server_url="https://s.example.com")
    assert script.startswith("#!/bin/sh")
    # StrictUndefined would have failed the render, but a variable missing from
    # a branch we do not exercise would still ship an empty string into a shell.
    assert "{{" not in script, "unrendered Jinja placeholder reached the script"
    assert "}}" not in script

    sh = shutil.which("sh")
    assert sh is not None
    check = subprocess.run(  # noqa: S603 - fixed argv, input is our own template
        [sh, "-n"], input=script, text=True, capture_output=True, check=False
    )
    assert check.returncode == 0, check.stderr


async def test_uninstall_script_is_served_with_the_install_paths(client: AsyncClient) -> None:
    r = await client.get("/uninstall.sh")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/x-shellscript")
    # The installer and uninstaller are two halves of one contract about what
    # lives where on a host, so they must name the same paths.
    from sum_server.install.service import BIN_PATH, ENV_FILE, STATE_DIR, UNIT_PATH

    for path in (BIN_PATH, ENV_FILE, STATE_DIR, UNIT_PATH):
        assert path in r.text


async def test_uninstall_script_needs_no_auth_and_no_release(client: AsyncClient) -> None:
    """Unlike install.sh it resolves nothing, so it works on a server with no
    known agent release and for a host whose credentials are already gone."""
    r = await client.get("/uninstall.sh")
    assert r.status_code == 200
    assert "sum-agent removed from this host" in r.text
