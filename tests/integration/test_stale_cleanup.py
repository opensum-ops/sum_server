"""Expired-enrollment cleanup: what it deletes, and what it must not.

This is the first thing in the project that destroys data with no human in the
loop, so the tests that matter are the negative ones. Every way a host can be
real is asserted to survive: enrolled once, heartbeated once, enrollment still
inside the grace period, no enrollment ever.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy import func, select

from sum_server.hosts import cleanup
from sum_server.hosts.models import Host
from tests.conftest import auth_h

DAY = 86400
GRACE = 7 * DAY


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


async def _mk_host(client: AsyncClient, token: str, hostname: str) -> str:
    r = await client.post("/api/v1/hosts", headers=auth_h(token), json={"hostname": hostname})
    assert r.status_code == 201, r.text
    host_id: str = r.json()["id"]
    return host_id


async def _mk_enrollment(client: AsyncClient, token: str, host_id: str) -> str:
    r = await client.post(
        "/api/v1/agents/enrollments", headers=auth_h(token), json={"host_id": host_id}
    )
    assert r.status_code in (200, 201), r.text
    raw: str = r.json()["enrollment_token"]
    return raw


async def _expire(db_session: Any, host_id: str, *, ago_seconds: int) -> None:
    """Backdate every enrollment for a host, standing in for the passage of time."""
    from sum_server.agents.models import AgentEnrollment

    await db_session.execute(
        AgentEnrollment.__table__.update()
        .where(AgentEnrollment.host_id == uuid.UUID(host_id))
        .values(expires_at=_now() - dt.timedelta(seconds=ago_seconds))
    )
    await db_session.commit()


async def _host_ids(db_session: Any) -> set[uuid.UUID]:
    rows = (await db_session.execute(select(Host.id))).scalars().all()
    return set(rows)


async def test_deletes_a_host_whose_enrollment_expired_unused(
    client: AsyncClient, admin_token: str, db_session: Any
) -> None:
    host_id = await _mk_host(client, admin_token, "never-showed-up")
    await _mk_enrollment(client, admin_token, host_id)
    await _expire(db_session, host_id, ago_seconds=GRACE + DAY)

    swept = await cleanup.sweep(db_session, grace_seconds=GRACE)
    await db_session.commit()

    assert [s.hostname for s in swept] == ["never-showed-up"]
    assert uuid.UUID(host_id) not in await _host_ids(db_session)


async def test_leaves_a_host_still_inside_the_grace_period(
    client: AsyncClient, admin_token: str, db_session: Any
) -> None:
    """The token stopping work is not the same as the operator giving up."""
    host_id = await _mk_host(client, admin_token, "installing-tomorrow")
    await _mk_enrollment(client, admin_token, host_id)
    await _expire(db_session, host_id, ago_seconds=DAY)  # expired, but recently

    assert await cleanup.sweep(db_session, grace_seconds=GRACE) == []
    assert uuid.UUID(host_id) in await _host_ids(db_session)


async def test_leaves_a_host_that_was_ever_enrolled(
    client: AsyncClient, admin_token: str, db_session: Any
) -> None:
    """The trap this rule exists to avoid: presence returns to `pending` after
    an agent removal, so a real machine looks exactly like one that never
    arrived. Only an agent token that once existed tells them apart."""
    host_id = await _mk_host(client, admin_token, "was-real")
    raw = await _mk_enrollment(client, admin_token, host_id)
    en = await client.post("/api/v1/agents/enroll", json={"enrollment_token": raw})
    assert en.status_code == 200, en.text
    await _expire(db_session, host_id, ago_seconds=GRACE + 30 * DAY)

    assert await cleanup.sweep(db_session, grace_seconds=GRACE) == []
    assert uuid.UUID(host_id) in await _host_ids(db_session)


async def test_leaves_a_host_that_has_ever_heartbeated(
    client: AsyncClient, admin_token: str, db_session: Any
) -> None:
    """A second, independent way of asking whether the host was ever alive."""
    host_id = await _mk_host(client, admin_token, "spoke-once")
    await _mk_enrollment(client, admin_token, host_id)
    await _expire(db_session, host_id, ago_seconds=GRACE + DAY)
    await db_session.execute(
        Host.__table__.update()
        .where(Host.id == uuid.UUID(host_id))
        .values(last_heartbeat_at=_now() - dt.timedelta(days=400))
    )
    await db_session.commit()

    assert await cleanup.sweep(db_session, grace_seconds=GRACE) == []
    assert uuid.UUID(host_id) in await _host_ids(db_session)


async def test_leaves_a_host_with_no_enrollment_at_all(
    client: AsyncClient, admin_token: str, db_session: Any
) -> None:
    """No enrollment means no enrollment period to have expired. Deleting on
    the absence of evidence would take out hosts created by hand or by API."""
    host_id = await _mk_host(client, admin_token, "created-by-api")

    assert await cleanup.sweep(db_session, grace_seconds=GRACE) == []
    assert uuid.UUID(host_id) in await _host_ids(db_session)


async def test_the_newest_enrollment_decides(
    client: AsyncClient, admin_token: str, db_session: Any
) -> None:
    """Re-issuing a token restarts the clock; an old expired one alongside it
    must not drag the host into the sweep."""
    from sum_server.agents.models import AgentEnrollment

    host_id = await _mk_host(client, admin_token, "retried")
    await _mk_enrollment(client, admin_token, host_id)
    await _expire(db_session, host_id, ago_seconds=GRACE + 10 * DAY)
    await _mk_enrollment(client, admin_token, host_id)  # fresh, still valid

    rows = (
        await db_session.execute(
            select(func.count())
            .select_from(AgentEnrollment)
            .where(AgentEnrollment.host_id == uuid.UUID(host_id))
        )
    ).scalar_one()
    assert rows == 2

    assert await cleanup.sweep(db_session, grace_seconds=GRACE) == []
    assert uuid.UUID(host_id) in await _host_ids(db_session)


async def test_writes_one_audit_entry_per_deletion_and_keeps_it(
    client: AsyncClient, admin_token: str, db_session: Any
) -> None:
    """Hard constraint #5, and the only remaining record of what was deleted.
    ``audit_entries.target_id`` is not a foreign key precisely so the entry
    outlives the row it describes."""
    from sum_server.core.audit import AuditEntry

    host_id = await _mk_host(client, admin_token, "gone-but-logged")
    await _mk_enrollment(client, admin_token, host_id)
    await _expire(db_session, host_id, ago_seconds=GRACE + DAY)

    await cleanup.sweep(db_session, grace_seconds=GRACE)
    await db_session.commit()

    entry = (
        await db_session.execute(
            select(AuditEntry).where(
                AuditEntry.action == "host.deleted",
                AuditEntry.target_id == uuid.UUID(host_id),
            )
        )
    ).scalar_one()
    assert entry.actor_kind == "system"
    assert entry.actor_id is None
    assert entry.payload["reason"] == cleanup.DELETE_REASON
    assert entry.payload["hostname"] == "gone-but-logged"


async def test_an_operator_delete_stays_distinguishable_in_the_audit_log(
    client: AsyncClient, admin_token: str, db_session: Any
) -> None:
    """Both paths hard-delete the same way, so the payload's reason is the only
    thing that still says whether a person decided."""
    from sum_server.core.audit import AuditEntry
    from sum_server.hosts import removal

    host_id = await _mk_host(client, admin_token, "deleted-by-hand")
    host = (
        await db_session.execute(select(Host).where(Host.id == uuid.UUID(host_id)))
    ).scalar_one()
    from sum_server.core.context import Actor

    await removal.delete_host_record(
        db_session, host=host, actor=Actor(kind="user", id=uuid.uuid4())
    )
    await db_session.commit()

    entry = (
        await db_session.execute(
            select(AuditEntry).where(
                AuditEntry.action == "host.deleted", AuditEntry.target_id == uuid.UUID(host_id)
            )
        )
    ).scalar_one()
    assert entry.payload["reason"] == "never_enrolled"
    assert entry.actor_kind == "user"


async def test_sweeping_twice_is_a_no_op(
    client: AsyncClient, admin_token: str, db_session: Any
) -> None:
    host_id = await _mk_host(client, admin_token, "swept-once")
    await _mk_enrollment(client, admin_token, host_id)
    await _expire(db_session, host_id, ago_seconds=GRACE + DAY)

    assert len(await cleanup.sweep(db_session, grace_seconds=GRACE)) == 1
    await db_session.commit()
    assert await cleanup.sweep(db_session, grace_seconds=GRACE) == []
    await db_session.commit()


async def test_the_wizard_says_the_record_will_be_cleaned_up(
    client: AsyncClient, admin_user: Any, monkeypatch: Any
) -> None:
    """An automatic deletion the operator was never told about is a surprise.
    The wizard is the one moment they are looking at the enrollment.

    The sweep is off across the suite (see conftest), so the enabled case has
    to be asked for explicitly here.
    """
    from sum_server.settings import get_settings
    from sum_server.ui import routes as ui_routes
    from tests.integration.test_ui import _ui_login

    on = get_settings().model_copy(update={"stale_host_cleanup_enabled": True})
    monkeypatch.setattr(ui_routes, "get_settings", lambda: on)

    csrf = await _ui_login(client, "admin@example.com", "admin-pw-1234")
    r = await client.post(
        "/hosts/enroll",
        data={"csrf_token": csrf, "hostname": "wizard-host", "description": ""},
    )
    assert r.status_code == 200, r.text
    assert "deleted automatically" in r.text
