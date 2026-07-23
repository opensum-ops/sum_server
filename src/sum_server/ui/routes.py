"""Web UI routes: login/logout + server-rendered pages."""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from sum_server.auth import service as auth_svc
from sum_server.components import service as components_svc
from sum_server.core.audit import AuditEntry
from sum_server.core.db import SessionDep
from sum_server.core.errors import AuthError, ForbiddenError, NotFoundError
from sum_server.core.pagination import Cursor
from sum_server.servers import service as servers_svc
from sum_server.settings import Env, get_settings
from sum_server.ui.deps import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    UiUser,
    check_csrf,
    new_csrf_token,
)

router = APIRouter(tags=["ui"], include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _cookie_secure() -> bool:
    return get_settings().env is Env.prod


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    """Render the login form and ensure the CSRF cookie is set."""
    csrf = request.cookies.get(CSRF_COOKIE) or new_csrf_token()
    resp: HTMLResponse = templates.TemplateResponse(
        request, "login.html", {"csrf_token": csrf, "error": None}
    )
    resp.set_cookie(CSRF_COOKIE, csrf, samesite="lax", secure=_cookie_secure())
    return resp


@router.post("/login", response_class=HTMLResponse, response_model=None)
async def login_submit(
    request: Request,
    session: SessionDep,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()],
    next_path: Annotated[str, Form(alias="next")] = "/servers",
) -> HTMLResponse | RedirectResponse:
    check_csrf(request, csrf_token)
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    try:
        async with session.begin():
            raw, sess = await auth_svc.login(
                session, email=email, password=password, ip=ip, user_agent=ua
            )
    except AuthError:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"csrf_token": csrf_token, "error": "Invalid email or password."},
            status_code=401,
        )
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = "/servers"
    max_age = max(0, int((sess.expires_at - dt.datetime.now(tz=dt.UTC)).total_seconds()))
    resp = RedirectResponse(next_path, status_code=303)
    resp.set_cookie(
        SESSION_COOKIE,
        raw,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(),
    )
    return resp


@router.post("/logout")
async def logout(
    request: Request,
    session: SessionDep,
    _user: UiUser,
    csrf_token: Annotated[str, Form()],
) -> RedirectResponse:
    check_csrf(request, csrf_token)
    raw = request.cookies.get(SESSION_COOKIE)
    if raw:
        async with session.begin():
            await auth_svc.logout(session, raw_token=raw)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@router.get("/", response_class=RedirectResponse)
async def root(_user: UiUser) -> RedirectResponse:
    return RedirectResponse("/servers", status_code=303)


async def _render(
    request: Request,
    session: SessionDep,
    actor_id: uuid.UUID,
    template: str,
    active: str,
    extra: dict[str, object],
) -> HTMLResponse:
    """Render a page template with the base-shell context (and heal the CSRF cookie)."""
    from sum_server.users.service import get_user

    user = await get_user(session, actor_id)
    csrf = request.cookies.get(CSRF_COOKIE) or new_csrf_token()
    ctx: dict[str, object] = {
        "csrf_token": csrf,
        "user_email": user.email if user else "",
        "is_admin": bool(user and user.is_admin),
        "active": active,
        **extra,
    }
    resp: HTMLResponse = templates.TemplateResponse(request, template, ctx)
    if CSRF_COOKIE not in request.cookies:
        resp.set_cookie(CSRF_COOKIE, csrf, samesite="lax", secure=_cookie_secure())
    return resp


@router.get("/servers", response_class=HTMLResponse)
async def servers_page(request: Request, session: SessionDep, user: UiUser) -> HTMLResponse:
    assert user.id is not None
    rows = await servers_svc.list_servers_visible_to(
        session, actor_user_id=user.id, limit=200, cursor=None
    )
    return await _render(
        request, session, user.id, "servers.html", "servers", {"servers": rows[:200]}
    )


@router.get("/servers/{server_id}", response_class=HTMLResponse)
async def server_detail_page(
    request: Request,
    session: SessionDep,
    user: UiUser,
    server_id: uuid.UUID,
) -> HTMLResponse:
    assert user.id is not None
    server = await servers_svc.get_server(session, server_id)
    if server is None:
        raise NotFoundError("server not found")
    if not await servers_svc.user_can_read(session, server, user.id):
        raise ForbiddenError("not visible")

    from sum_server.teams.service import get_team
    from sum_server.users.service import get_user

    components = await components_svc.list_components(
        session, server_id=server_id, include_absent=True
    )
    user_owners = [
        u
        for uid in await servers_svc.get_user_owner_ids(session, server_id)
        if (u := await get_user(session, uid)) is not None
    ]
    team_owners = [
        t
        for tid in await servers_svc.get_team_owner_ids(session, server_id)
        if (t := await get_team(session, tid)) is not None
    ]
    return await _render(
        request,
        session,
        user.id,
        "server_detail.html",
        "servers",
        {
            "server": server,
            "components": components,
            "user_owners": user_owners,
            "team_owners": team_owners,
        },
    )


async def _require_can_manage(
    session: SessionDep, server_id: uuid.UUID, actor_id: uuid.UUID
) -> None:
    """Owner-or-admin gate for UI write actions (mirrors the API helpers)."""
    server = await servers_svc.get_server(session, server_id)
    if server is None:
        raise NotFoundError("server not found")
    ok = await servers_svc.user_can_read(session, server, actor_id)
    # Release the auto-begun read transaction so the handler owns the write.
    await session.rollback()
    if not ok:
        raise ForbiddenError("not authorized for this server")


@router.post("/servers/{server_id}/owners/add")
async def owners_add(
    request: Request,
    session: SessionDep,
    user: UiUser,
    server_id: uuid.UUID,
    csrf_token: Annotated[str, Form()],
    owner_kind: Annotated[str, Form()],
    identifier: Annotated[str, Form()],
) -> RedirectResponse:
    check_csrf(request, csrf_token)
    assert user.id is not None
    await _require_can_manage(session, server_id, user.id)

    from sum_server.teams.service import get_team_by_name
    from sum_server.users.service import get_user_by_email

    async with session.begin():
        if owner_kind == "team":
            team = await get_team_by_name(session, identifier.strip())
            if team is None:
                raise NotFoundError("team not found")
            await servers_svc.add_team_owner(session, server_id=server_id, team_id=team.id)
        else:
            owner = await get_user_by_email(session, identifier.strip().lower())
            if owner is None:
                raise NotFoundError("user not found")
            await servers_svc.add_user_owner(session, server_id=server_id, user_id=owner.id)
    return RedirectResponse(f"/servers/{server_id}", status_code=303)


@router.post("/servers/{server_id}/owners/remove")
async def owners_remove(
    request: Request,
    session: SessionDep,
    user: UiUser,
    server_id: uuid.UUID,
    csrf_token: Annotated[str, Form()],
    owner_kind: Annotated[str, Form()],
    owner_id: Annotated[uuid.UUID, Form()],
) -> RedirectResponse:
    check_csrf(request, csrf_token)
    assert user.id is not None
    await _require_can_manage(session, server_id, user.id)
    async with session.begin():
        if owner_kind == "team":
            await servers_svc.remove_team_owner(session, server_id=server_id, team_id=owner_id)
        else:
            await servers_svc.remove_user_owner(session, server_id=server_id, user_id=owner_id)
    return RedirectResponse(f"/servers/{server_id}", status_code=303)


async def _require_admin_ui(session: SessionDep, actor_id: uuid.UUID) -> None:
    from sum_server.users.service import get_user

    user = await get_user(session, actor_id)
    if user is None or not user.is_admin:
        raise ForbiddenError("admin-only page")


async def _audit_query(
    session: SessionDep,
    *,
    action: str | None,
    target_kind: str | None,
    cursor: Cursor | None = None,
    limit: int = 50,
) -> tuple[list[AuditEntry], str | None]:
    stmt = select(AuditEntry)
    if action:
        stmt = stmt.where(AuditEntry.action == action)
    if target_kind:
        stmt = stmt.where(AuditEntry.target_kind == target_kind)
    if cursor is not None:
        stmt = stmt.where(
            (AuditEntry.ts, AuditEntry.id) < (cursor.ts, cursor.id)  # type: ignore[operator]
        )
    stmt = stmt.order_by(AuditEntry.ts.desc(), AuditEntry.id.desc()).limit(limit + 1)
    rows = list((await session.execute(stmt)).scalars().all())
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = (
        Cursor(id=items[-1].id, ts=items[-1].ts).encode() if (has_more and items) else None
    )
    return items, next_cursor


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(
    request: Request,
    session: SessionDep,
    user: UiUser,
    action: str | None = None,
    target_kind: str | None = None,
) -> HTMLResponse:
    assert user.id is not None
    await _require_admin_ui(session, user.id)
    entries, next_cursor = await _audit_query(
        session, action=action or None, target_kind=target_kind or None
    )
    return await _render(
        request,
        session,
        user.id,
        "audit.html",
        "audit",
        {
            "entries": entries,
            "next_cursor": next_cursor,
            "action_filter": action or "",
            "target_kind_filter": target_kind or "",
        },
    )


@router.get("/audit/rows", response_class=HTMLResponse)
async def audit_rows(
    request: Request,
    session: SessionDep,
    user: UiUser,
    cursor: str,
    action: str | None = None,
    target_kind: str | None = None,
) -> HTMLResponse:
    assert user.id is not None
    await _require_admin_ui(session, user.id)
    try:
        cur = Cursor.decode(cursor)
    except ValueError:
        raise NotFoundError("bad cursor") from None
    entries, next_cursor = await _audit_query(
        session, action=action or None, target_kind=target_kind or None, cursor=cur
    )
    return templates.TemplateResponse(
        request,
        "_audit_rows.html",
        {
            "entries": entries,
            "next_cursor": next_cursor,
            "action_filter": action or "",
            "target_kind_filter": target_kind or "",
        },
    )
