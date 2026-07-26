"""Web UI routes: login/logout + server-rendered pages (hosts, groups, audit)."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from sum_server import __version__
from sum_server.auth import service as auth_svc
from sum_server.components import service as components_svc
from sum_server.core.audit import AuditEntry
from sum_server.core.db import SessionDep
from sum_server.core.errors import AuthError, ForbiddenError, NotFoundError
from sum_server.core.pagination import Cursor
from sum_server.hosts import service as hosts_svc
from sum_server.hosts.presence import PRESENCE_VALUES
from sum_server.hosts.search import HostSearch, HostSearchResult, search_hosts
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

# Cache-buster for the static assets. Pages are dynamic and never cached, but
# StaticFiles sets no Cache-Control, so browsers cache CSS and JS heuristically.
# Without this, an update serves new markup against the previous stylesheet.
# A global rather than per-route context: login.html does not go through
# _render, and every future template gets it for free.
templates.env.globals["asset_version"] = __version__


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
    next_path: Annotated[str, Form(alias="next")] = "/hosts",
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
        next_path = "/hosts"
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
    return RedirectResponse("/hosts", status_code=303)


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


# --- Hosts: search + list ---------------------------------------------------


def _split_kv(raw: str) -> tuple[str, str] | None:
    if ":" not in raw:
        return None
    key, value = raw.split(":", 1)
    key, value = key.strip(), value.strip()
    return (key, value) if key else None


def _parse_search(request: Request) -> HostSearch:
    qp = request.query_params
    facts = tuple(kv for f in qp.getlist("fact") if (kv := _split_kv(f)) is not None)
    params = tuple(kv for p in qp.getlist("param") if (kv := _split_kv(p)) is not None)
    return HostSearch(
        text=(qp.get("q") or "").strip() or None,
        presence=qp.get("presence") or None,
        group=qp.get("group") or None,
        component=(qp.get("component") or "").strip() or None,
        facts=facts,
        parameters=params,
    )


def _filter_chips(search: HostSearch) -> list[dict[str, str]]:
    """Active filters as chips, each with a URL that removes just itself.

    Rendered server-side so the bar works without JavaScript; ``app.js`` takes
    over chip rendering once loaded, using the same markup.
    """
    pairs: list[tuple[str, str, str]] = []  # (query key, query value, label)
    if search.text:
        pairs.append(("q", search.text, f'text: "{search.text}"'))
    if search.presence:
        pairs.append(("presence", search.presence, f"state: {search.presence}"))
    if search.group:
        pairs.append(("group", search.group, f"group: {search.group}"))
    if search.component:
        pairs.append(("component", search.component, f"component: {search.component}"))
    for key, value in search.facts:
        pairs.append(("fact", f"{key}:{value}", f"fact {key} = {value}"))
    for key, value in search.parameters:
        pairs.append(("param", f"{key}:{value}", f"param {key} = {value}"))

    chips: list[dict[str, str]] = []
    for i, (_, _, label) in enumerate(pairs):
        rest = [(k, v) for j, (k, v, _) in enumerate(pairs) if j != i]
        chips.append({"label": label, "remove_url": "/hosts?" + urlencode(rest)})
    return chips


def _search_to_tokens(search: HostSearch) -> str:
    """Serialize a parsed search back into the bar's token grammar.

    Grammar: ``presence:v``, ``group:v``, ``component:v``, ``fact:k=v``,
    ``param:k=v``, and bare words for free text. ``app.js`` parses the same
    shape and maps it back onto the query params.
    """
    parts: list[str] = []
    if search.presence:
        parts.append(f"presence:{search.presence}")
    if search.group:
        parts.append(f"group:{search.group}")
    if search.component:
        parts.append(f"component:{search.component}")
    parts += [f"fact:{k}={v}" for k, v in search.facts]
    parts += [f"param:{k}={v}" for k, v in search.parameters]
    if search.text:
        parts.append(search.text)
    return " ".join(parts)


def _results_context(search: HostSearch, results: list[HostSearchResult]) -> dict[str, object]:
    """Context shared by the full page and the ``/hosts/rows`` partial, so the
    two renderings cannot drift apart.
    """
    return {
        "results": results,
        "search": search,
        "fact_keys": [k for k, _ in search.facts],
        "param_keys": [k for k, _ in search.parameters],
        "has_filters": search != HostSearch(),
    }


@router.get("/hosts", response_class=HTMLResponse)
async def hosts_page(request: Request, session: SessionDep, user: UiUser) -> HTMLResponse:
    assert user.id is not None
    search = _parse_search(request)
    results = await search_hosts(session, actor_user_id=user.id, search=search)
    return await _render(
        request,
        session,
        user.id,
        "hosts.html",
        "hosts",
        {
            **_results_context(search, results),
            "chips": _filter_chips(search),
            "search_tokens": _search_to_tokens(search),
        },
    )


@router.get("/hosts/rows", response_class=HTMLResponse)
async def hosts_rows(request: Request, session: SessionDep, user: UiUser) -> HTMLResponse:
    """Result rows only, for the live-search swap (see ``/audit/rows``)."""
    assert user.id is not None
    search = _parse_search(request)
    results = await search_hosts(session, actor_user_id=user.id, search=search)
    return templates.TemplateResponse(request, "_host_rows.html", _results_context(search, results))


# Search-bar vocabulary: (token prefix, hint shown beside it).
_SEARCH_FIELDS: tuple[tuple[str, str], ...] = (
    ("presence:", "live state"),
    ("group:", "group membership"),
    ("fact:", "agent-observed key=value"),
    ("param:", "assigned key=value"),
    ("component:", "hardware substring"),
)


async def _suggestions_for(
    session: SessionDep, *, actor_user_id: uuid.UUID, token: str
) -> list[dict[str, str]]:
    """Suggestions for the token being typed. See ``_search_to_tokens`` for the grammar."""
    from sum_server.groups.service import distinct_parameter_keys, list_groups
    from sum_server.hosts.facts import distinct_fact_keys, distinct_fact_values

    lowered = token.lower()
    if ":" not in token:
        return [
            {"value": prefix, "label": prefix, "hint": hint}
            for prefix, hint in _SEARCH_FIELDS
            if prefix.startswith(lowered)
        ]

    field, rest = token.split(":", 1)
    field = field.lower()

    if field == "presence":
        return [
            {"value": f"presence:{p}", "label": p, "hint": ""}
            for p in PRESENCE_VALUES
            if p.startswith(rest.lower())
        ]
    if field == "group":
        return [
            {"value": f"group:{g.name}", "label": g.name, "hint": g.description or ""}
            for g in await list_groups(session)
            if g.name.lower().startswith(rest.lower())
        ]
    if field in ("fact", "param"):
        if "=" not in rest:
            keys = (
                await distinct_fact_keys(session, actor_user_id=actor_user_id, prefix=rest)
                if field == "fact"
                else await distinct_parameter_keys(
                    session, actor_user_id=actor_user_id, prefix=rest
                )
            )
            return [{"value": f"{field}:{k}=", "label": k, "hint": "pick a value"} for k in keys]
        if field == "fact":
            key, value_prefix = rest.split("=", 1)
            return [
                {"value": f"fact:{key}={v}", "label": v, "hint": key}
                for v in await distinct_fact_values(
                    session, actor_user_id=actor_user_id, key=key, prefix=value_prefix
                )
            ]
    return []


@router.get("/hosts/suggest", response_class=HTMLResponse)
async def hosts_suggest(
    request: Request, session: SessionDep, user: UiUser, token: str = ""
) -> HTMLResponse:
    """Suggestion list for the search bar. Values are visibility-scoped."""
    assert user.id is not None
    suggestions = await _suggestions_for(session, actor_user_id=user.id, token=token.strip())
    return templates.TemplateResponse(request, "_suggestions.html", {"suggestions": suggestions})


# --- Hosts: enrollment wizard ----------------------------------------------
# Registered before /hosts/{host_id} so the literal path wins.


def _server_url(request: Request) -> str:
    configured = get_settings().external_url.strip()
    return configured.rstrip("/") if configured else str(request.base_url).rstrip("/")


@router.get("/hosts/enroll", response_class=HTMLResponse)
async def host_enroll_form(request: Request, session: SessionDep, user: UiUser) -> HTMLResponse:
    assert user.id is not None
    await _require_admin_ui(session, user.id)
    return await _render(request, session, user.id, "host_enroll_form.html", "hosts", {})


@router.post("/hosts/enroll", response_class=HTMLResponse)
async def host_enroll_create(
    request: Request,
    session: SessionDep,
    user: UiUser,
    csrf_token: Annotated[str, Form()],
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    ttl_seconds: Annotated[int, Form()] = 3600,
) -> HTMLResponse:
    from sum_server.agents.service import create_enrollment
    from sum_server.hosts.schemas import HostCreate

    check_csrf(request, csrf_token)
    assert user.id is not None
    await _require_admin_ui(session, user.id)
    host_name = name.strip() or f"host-{dt.datetime.now(tz=dt.UTC):%Y%m%d-%H%M%S}"
    ttl = min(max(ttl_seconds, 60), 86400)
    async with session.begin():
        host = await hosts_svc.create_host(
            session,
            HostCreate(name=host_name, description=description.strip() or None),
        )
        raw, enr = await create_enrollment(session, host_id=host.id, actor=user, ttl_seconds=ttl)
        host_id, expires_at = host.id, enr.expires_at
    host2 = await hosts_svc.get_host(session, host_id)
    return await _render(
        request,
        session,
        user.id,
        "host_enroll.html",
        "hosts",
        {
            "host": host2,
            "enrollment_token": raw,
            "expires_at": expires_at,
            "server_url": _server_url(request),
        },
    )


@router.post("/hosts/{host_id}/enroll-token", response_class=HTMLResponse)
async def host_enroll_token(
    request: Request,
    session: SessionDep,
    user: UiUser,
    host_id: uuid.UUID,
    csrf_token: Annotated[str, Form()],
) -> HTMLResponse:
    from sum_server.agents.service import create_enrollment

    check_csrf(request, csrf_token)
    assert user.id is not None
    await _require_admin_ui(session, user.id)
    async with session.begin():
        raw, enr = await create_enrollment(session, host_id=host_id, actor=user, ttl_seconds=None)
        expires_at = enr.expires_at
    host = await hosts_svc.get_host(session, host_id)
    if host is None:
        raise NotFoundError("host not found")
    return await _render(
        request,
        session,
        user.id,
        "host_enroll.html",
        "hosts",
        {
            "host": host,
            "enrollment_token": raw,
            "expires_at": expires_at,
            "server_url": _server_url(request),
        },
    )


# --- Hosts: detail (tabbed) -------------------------------------------------

_HOST_TABS = ("overview", "storage", "network", "hardware", "groups")


@router.get("/hosts/{host_id}", response_class=HTMLResponse)
async def host_detail_page(
    request: Request,
    session: SessionDep,
    user: UiUser,
    host_id: uuid.UUID,
    tab: str = "overview",
) -> HTMLResponse:
    from sum_server.groups.service import (
        effective_parameters_for_host,
        list_groups,
        list_groups_for_host,
        list_host_parameters,
    )
    from sum_server.hosts.facts import list_facts
    from sum_server.teams.service import get_team
    from sum_server.users.service import get_user

    assert user.id is not None
    host = await hosts_svc.get_host(session, host_id)
    if host is None:
        raise NotFoundError("host not found")
    if not await hosts_svc.user_can_read(session, host, user.id):
        raise ForbiddenError("not visible")
    if tab not in _HOST_TABS:
        tab = "overview"

    components = await components_svc.list_components(session, host_id=host_id, include_absent=True)
    by_kind: dict[str, list[object]] = {}
    for c in components:
        by_kind.setdefault(c.kind, []).append(c)
    facts = await list_facts(session, host_id=host_id)
    facts_map = {f.key: f.value for f in facts}
    member_groups = await list_groups_for_host(session, host_id=host_id)
    member_ids = {g.id for g in member_groups}
    addable_groups = [
        g for g in await list_groups(session) if g.id not in member_ids and g.name != "global"
    ]
    host_params = await list_host_parameters(session, host_id=host_id)
    effective = await effective_parameters_for_host(session, host_id=host_id)
    user_owners = [
        u
        for uid in await hosts_svc.get_user_owner_ids(session, host_id)
        if (u := await get_user(session, uid)) is not None
    ]
    team_owners = [
        t
        for tid in await hosts_svc.get_team_owner_ids(session, host_id)
        if (t := await get_team(session, tid)) is not None
    ]

    from sum_server.core.versions import is_newer
    from sum_server.updates.models import COMPONENT_AGENT
    from sum_server.updates.service import get_release_cache

    agent_current = str(facts_map.get("agent_version") or "")
    agent_cache = await get_release_cache(session, COMPONENT_AGENT)
    latest_agent = agent_cache.latest_version if agent_cache else None
    return await _render(
        request,
        session,
        user.id,
        "host_detail.html",
        "hosts",
        {
            "host": host,
            "tab": tab,
            "tabs": _HOST_TABS,
            "components_by_kind": by_kind,
            "facts": facts,
            "facts_map": facts_map,
            "member_groups": member_groups,
            "addable_groups": addable_groups,
            "host_params": host_params,
            "effective_params": effective,
            "user_owners": user_owners,
            "team_owners": team_owners,
            "agent_current": agent_current,
            "latest_agent": latest_agent,
            "agent_update_available": is_newer(latest_agent, agent_current),
            "target_agent_version": host.target_agent_version,
        },
    )


@router.post("/hosts/{host_id}/agent-update")
async def host_agent_update(
    request: Request,
    session: SessionDep,
    user: UiUser,
    host_id: uuid.UUID,
    csrf_token: Annotated[str, Form()],
    target_version: Annotated[str, Form()],
) -> RedirectResponse:
    from sum_server.core.errors import ConflictError
    from sum_server.updates import agent_binary, agent_update

    check_csrf(request, csrf_token)
    assert user.id is not None
    await _require_admin_ui(session, user.id)
    # Cache the binary now (slow path, with feedback) so heartbeats stay fast.
    try:
        await agent_binary.ensure_cached(session, target_version)
    except agent_binary.BinaryUnavailableError as exc:
        raise ConflictError(f"cannot stage agent {target_version}: {exc}") from exc
    async with session.begin():
        host = await hosts_svc.get_host(session, host_id)
        if host is None:
            raise NotFoundError("host not found")
        await agent_update.set_target(session, host=host, version=target_version, actor=user)
    return RedirectResponse(f"/hosts/{host_id}", status_code=303)


@router.post("/hosts/{host_id}/agent-update/cancel")
async def host_agent_update_cancel(
    request: Request,
    session: SessionDep,
    user: UiUser,
    host_id: uuid.UUID,
    csrf_token: Annotated[str, Form()],
) -> RedirectResponse:
    from sum_server.updates import agent_update

    check_csrf(request, csrf_token)
    assert user.id is not None
    await _require_admin_ui(session, user.id)
    async with session.begin():
        host = await hosts_svc.get_host(session, host_id)
        if host is None:
            raise NotFoundError("host not found")
        if host.target_agent_version:
            await agent_update.clear_target(session, host=host, reason="cancelled")
    return RedirectResponse(f"/hosts/{host_id}", status_code=303)


# --- Hosts: parameter + group membership actions ----------------------------


def _parse_param_value(raw: str) -> object:
    """JSON if it parses, plain string otherwise."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


@router.post("/hosts/{host_id}/params/set")
async def host_param_set(
    request: Request,
    session: SessionDep,
    user: UiUser,
    host_id: uuid.UUID,
    csrf_token: Annotated[str, Form()],
    key: Annotated[str, Form()],
    value: Annotated[str, Form()],
) -> RedirectResponse:
    from sum_server.groups.routes import checked_param_key
    from sum_server.groups.service import set_host_parameter

    check_csrf(request, csrf_token)
    assert user.id is not None
    key = checked_param_key(key.strip())
    await _require_can_manage(session, host_id, user.id)
    async with session.begin():
        await set_host_parameter(session, host_id=host_id, key=key, value=_parse_param_value(value))
    return RedirectResponse(f"/hosts/{host_id}?tab=groups", status_code=303)


@router.post("/hosts/{host_id}/params/delete")
async def host_param_delete(
    request: Request,
    session: SessionDep,
    user: UiUser,
    host_id: uuid.UUID,
    csrf_token: Annotated[str, Form()],
    key: Annotated[str, Form()],
) -> RedirectResponse:
    from sum_server.groups.service import unset_host_parameter

    check_csrf(request, csrf_token)
    assert user.id is not None
    await _require_can_manage(session, host_id, user.id)
    async with session.begin():
        await unset_host_parameter(session, host_id=host_id, key=key)
    return RedirectResponse(f"/hosts/{host_id}?tab=groups", status_code=303)


@router.post("/hosts/{host_id}/groups/add")
async def host_group_add(
    request: Request,
    session: SessionDep,
    user: UiUser,
    host_id: uuid.UUID,
    csrf_token: Annotated[str, Form()],
    group_id: Annotated[uuid.UUID, Form()],
) -> RedirectResponse:
    from sum_server.groups.service import add_member

    check_csrf(request, csrf_token)
    assert user.id is not None
    await _require_admin_ui(session, user.id)
    async with session.begin():
        await add_member(session, group_id=group_id, host_id=host_id)
    return RedirectResponse(f"/hosts/{host_id}?tab=groups", status_code=303)


@router.post("/hosts/{host_id}/groups/remove")
async def host_group_remove(
    request: Request,
    session: SessionDep,
    user: UiUser,
    host_id: uuid.UUID,
    csrf_token: Annotated[str, Form()],
    group_id: Annotated[uuid.UUID, Form()],
) -> RedirectResponse:
    from sum_server.groups.service import remove_member

    check_csrf(request, csrf_token)
    assert user.id is not None
    await _require_admin_ui(session, user.id)
    async with session.begin():
        await remove_member(session, group_id=group_id, host_id=host_id)
    return RedirectResponse(f"/hosts/{host_id}?tab=groups", status_code=303)


async def _require_can_manage(session: SessionDep, host_id: uuid.UUID, actor_id: uuid.UUID) -> None:
    """Owner-or-admin gate for UI write actions (mirrors the API helpers)."""
    host = await hosts_svc.get_host(session, host_id)
    if host is None:
        raise NotFoundError("host not found")
    ok = await hosts_svc.user_can_read(session, host, actor_id)
    # Release the auto-begun read transaction so the handler owns the write.
    await session.rollback()
    if not ok:
        raise ForbiddenError("not authorized for this host")


@router.post("/hosts/{host_id}/owners/add")
async def owners_add(
    request: Request,
    session: SessionDep,
    user: UiUser,
    host_id: uuid.UUID,
    csrf_token: Annotated[str, Form()],
    owner_kind: Annotated[str, Form()],
    identifier: Annotated[str, Form()],
) -> RedirectResponse:
    check_csrf(request, csrf_token)
    assert user.id is not None
    await _require_can_manage(session, host_id, user.id)

    from sum_server.teams.service import get_team_by_name
    from sum_server.users.service import get_user_by_email

    async with session.begin():
        if owner_kind == "team":
            team = await get_team_by_name(session, identifier.strip())
            if team is None:
                raise NotFoundError("team not found")
            await hosts_svc.add_team_owner(session, host_id=host_id, team_id=team.id)
        else:
            owner = await get_user_by_email(session, identifier.strip().lower())
            if owner is None:
                raise NotFoundError("user not found")
            await hosts_svc.add_user_owner(session, host_id=host_id, user_id=owner.id)
    return RedirectResponse(f"/hosts/{host_id}", status_code=303)


@router.post("/hosts/{host_id}/owners/remove")
async def owners_remove(
    request: Request,
    session: SessionDep,
    user: UiUser,
    host_id: uuid.UUID,
    csrf_token: Annotated[str, Form()],
    owner_kind: Annotated[str, Form()],
    owner_id: Annotated[uuid.UUID, Form()],
) -> RedirectResponse:
    check_csrf(request, csrf_token)
    assert user.id is not None
    await _require_can_manage(session, host_id, user.id)
    async with session.begin():
        if owner_kind == "team":
            await hosts_svc.remove_team_owner(session, host_id=host_id, team_id=owner_id)
        else:
            await hosts_svc.remove_user_owner(session, host_id=host_id, user_id=owner_id)
    return RedirectResponse(f"/hosts/{host_id}", status_code=303)


async def _require_admin_ui(session: SessionDep, actor_id: uuid.UUID) -> None:
    from sum_server.users.service import get_user

    ok = False
    try:
        user = await get_user(session, actor_id)
        # Read attributes before the rollback expires the instance.
        ok = user is not None and user.is_admin
    finally:
        # Release the auto-begun read transaction so write handlers can
        # open their own with ``async with session.begin()``.
        await session.rollback()
    if not ok:
        raise ForbiddenError("admin-only page")


# --- Groups pages -----------------------------------------------------------


@router.get("/groups", response_class=HTMLResponse)
async def groups_page(request: Request, session: SessionDep, user: UiUser) -> HTMLResponse:
    from sqlalchemy import func

    from sum_server.groups.models import Group, GroupParameter, host_groups
    from sum_server.groups.service import list_groups

    assert user.id is not None
    groups = await list_groups(session)
    children: dict[uuid.UUID | None, list[Group]] = {}
    for g in groups:
        children.setdefault(g.parent_id, []).append(g)

    member_counts: dict[uuid.UUID, int] = dict(
        (
            await session.execute(
                select(host_groups.c.group_id, func.count()).group_by(host_groups.c.group_id)
            )
        )
        .tuples()
        .all()
    )
    param_counts: dict[uuid.UUID, int] = dict(
        (
            await session.execute(
                select(GroupParameter.group_id, func.count()).group_by(GroupParameter.group_id)
            )
        )
        .tuples()
        .all()
    )

    rows: list[dict[str, object]] = []

    def _walk(parent_id: uuid.UUID | None, depth: int) -> None:
        for g in children.get(parent_id, []):
            rows.append(
                {
                    "group": g,
                    "depth": depth,
                    "members": member_counts.get(g.id, 0),
                    "params": param_counts.get(g.id, 0),
                }
            )
            _walk(g.id, depth + 1)

    _walk(None, 0)
    return await _render(
        request,
        session,
        user.id,
        "groups.html",
        "groups",
        {"rows": rows, "groups": groups},
    )


@router.post("/groups/create")
async def group_create(
    request: Request,
    session: SessionDep,
    user: UiUser,
    csrf_token: Annotated[str, Form()],
    name: Annotated[str, Form()],
    parent_id: Annotated[uuid.UUID, Form()],
    description: Annotated[str, Form()] = "",
) -> RedirectResponse:
    from sum_server.groups.schemas import GroupCreate
    from sum_server.groups.service import create_group

    check_csrf(request, csrf_token)
    assert user.id is not None
    await _require_admin_ui(session, user.id)
    payload = GroupCreate(name=name.strip(), description=description.strip() or None)
    async with session.begin():
        group = await create_group(
            session, name=payload.name, description=payload.description, parent_id=parent_id
        )
        group_id = group.id
    return RedirectResponse(f"/groups/{group_id}", status_code=303)


@router.get("/groups/{group_id}", response_class=HTMLResponse)
async def group_detail_page(
    request: Request,
    session: SessionDep,
    user: UiUser,
    group_id: uuid.UUID,
) -> HTMLResponse:
    from sqlalchemy import select as sa_select

    from sum_server.groups.service import (
        get_group,
        list_group_parameters,
        list_groups,
        list_member_host_ids,
    )
    from sum_server.hosts.models import Host

    assert user.id is not None
    group = await get_group(session, group_id)
    if group is None:
        raise NotFoundError("group not found")
    groups = await list_groups(session)
    parent = next((g for g in groups if g.id == group.parent_id), None)
    children = [g for g in groups if g.parent_id == group.id]
    params = await list_group_parameters(session, group_id=group_id)
    member_ids = await list_member_host_ids(session, group_id=group_id)
    members = (
        list(
            (
                await session.execute(
                    sa_select(Host).where(Host.id.in_(member_ids)).order_by(Host.name)
                )
            )
            .scalars()
            .all()
        )
        if member_ids
        else []
    )
    # Valid reparent targets: anything outside this group's own subtree.
    subtree = {group.id}
    changed = True
    while changed:
        changed = False
        for g in groups:
            if g.parent_id in subtree and g.id not in subtree:
                subtree.add(g.id)
                changed = True
    parent_options = [g for g in groups if g.id not in subtree]
    is_global = group.parent_id is None and group.name == "global"
    return await _render(
        request,
        session,
        user.id,
        "group_detail.html",
        "groups",
        {
            "group": group,
            "parent": parent,
            "children": children,
            "params": params,
            "members": members,
            "parent_options": parent_options,
            "is_global": is_global,
        },
    )


@router.post("/groups/{group_id}/update")
async def group_update(
    request: Request,
    session: SessionDep,
    user: UiUser,
    group_id: uuid.UUID,
    csrf_token: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    parent_id: Annotated[str, Form()] = "",
) -> RedirectResponse:
    from sum_server.groups.service import get_group, update_group

    check_csrf(request, csrf_token)
    assert user.id is not None
    await _require_admin_ui(session, user.id)
    current = await get_group(session, group_id)
    if current is None:
        raise NotFoundError("group not found")
    new_name = name.strip() or None
    new_parent = uuid.UUID(parent_id) if parent_id else None
    await session.rollback()
    async with session.begin():
        await update_group(
            session,
            group_id=group_id,
            name=new_name if new_name != current.name else None,
            description=description.strip() or None,
            parent_id=new_parent if new_parent != current.parent_id else None,
        )
    return RedirectResponse(f"/groups/{group_id}", status_code=303)


@router.post("/groups/{group_id}/delete")
async def group_delete(
    request: Request,
    session: SessionDep,
    user: UiUser,
    group_id: uuid.UUID,
    csrf_token: Annotated[str, Form()],
) -> RedirectResponse:
    from sum_server.groups.service import delete_group

    check_csrf(request, csrf_token)
    assert user.id is not None
    await _require_admin_ui(session, user.id)
    async with session.begin():
        await delete_group(session, group_id=group_id)
    return RedirectResponse("/groups", status_code=303)


@router.post("/groups/{group_id}/params/set")
async def group_param_set(
    request: Request,
    session: SessionDep,
    user: UiUser,
    group_id: uuid.UUID,
    csrf_token: Annotated[str, Form()],
    key: Annotated[str, Form()],
    value: Annotated[str, Form()],
) -> RedirectResponse:
    from sum_server.groups.routes import checked_param_key
    from sum_server.groups.service import set_group_parameter

    check_csrf(request, csrf_token)
    assert user.id is not None
    key = checked_param_key(key.strip())
    await _require_admin_ui(session, user.id)
    async with session.begin():
        await set_group_parameter(
            session, group_id=group_id, key=key, value=_parse_param_value(value)
        )
    return RedirectResponse(f"/groups/{group_id}", status_code=303)


@router.post("/groups/{group_id}/params/delete")
async def group_param_delete(
    request: Request,
    session: SessionDep,
    user: UiUser,
    group_id: uuid.UUID,
    csrf_token: Annotated[str, Form()],
    key: Annotated[str, Form()],
) -> RedirectResponse:
    from sum_server.groups.service import unset_group_parameter

    check_csrf(request, csrf_token)
    assert user.id is not None
    await _require_admin_ui(session, user.id)
    async with session.begin():
        await unset_group_parameter(session, group_id=group_id, key=key)
    return RedirectResponse(f"/groups/{group_id}", status_code=303)


@router.post("/groups/{group_id}/members/add")
async def group_member_add(
    request: Request,
    session: SessionDep,
    user: UiUser,
    group_id: uuid.UUID,
    csrf_token: Annotated[str, Form()],
    identifier: Annotated[str, Form()],
) -> RedirectResponse:
    from sqlalchemy import or_ as sa_or
    from sqlalchemy import select as sa_select

    from sum_server.groups.service import add_member
    from sum_server.hosts.models import Host

    check_csrf(request, csrf_token)
    assert user.id is not None
    await _require_admin_ui(session, user.id)
    ident = identifier.strip()
    host = (
        await session.execute(
            sa_select(Host).where(sa_or(Host.hostname == ident, Host.name == ident)).limit(1)
        )
    ).scalar_one_or_none()
    if host is None:
        raise NotFoundError("host not found")
    host_id = host.id
    await session.rollback()
    async with session.begin():
        await add_member(session, group_id=group_id, host_id=host_id)
    return RedirectResponse(f"/groups/{group_id}", status_code=303)


@router.post("/groups/{group_id}/members/remove")
async def group_member_remove(
    request: Request,
    session: SessionDep,
    user: UiUser,
    group_id: uuid.UUID,
    csrf_token: Annotated[str, Form()],
    host_id: Annotated[uuid.UUID, Form()],
) -> RedirectResponse:
    from sum_server.groups.service import remove_member

    check_csrf(request, csrf_token)
    assert user.id is not None
    await _require_admin_ui(session, user.id)
    async with session.begin():
        await remove_member(session, group_id=group_id, host_id=host_id)
    return RedirectResponse(f"/groups/{group_id}", status_code=303)


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


# --- Settings (admin) -------------------------------------------------------


async def _settings_context(session: SessionDep) -> dict[str, object]:
    from sum_server import __version__
    from sum_server.core.security import signing
    from sum_server.updates import service as updates_svc
    from sum_server.updates import system as system_svc
    from sum_server.updates.models import is_terminal

    settings = get_settings()
    summary = await updates_svc.build_summary(session)
    su_ok, su_reason = system_svc.self_update_available()
    latest = await system_svc.latest_update(session)
    in_progress = latest is not None and not is_terminal(latest.status)
    return {
        "version": __version__,
        "env": settings.env.value,
        "signing_loaded": signing.is_loaded(),
        "external_url": settings.external_url or "(request base URL)",
        "presence_online_window": settings.presence_online_window_seconds,
        "presence_reboot_grace": settings.presence_reboot_grace_seconds,
        "update_check_enabled": settings.update_check_enabled,
        "updates": summary,
        "self_update_available": su_ok,
        "self_update_reason": su_reason,
        "server_update": latest,
        "server_update_in_progress": in_progress,
    }


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, session: SessionDep, user: UiUser) -> HTMLResponse:
    assert user.id is not None
    await _require_admin_ui(session, user.id)
    ctx = await _settings_context(session)
    return await _render(request, session, user.id, "settings.html", "settings", ctx)


@router.post("/settings/check-updates")
async def settings_check_updates(
    request: Request,
    session: SessionDep,
    user: UiUser,
    csrf_token: Annotated[str, Form()],
) -> RedirectResponse:
    from sum_server.updates import service as updates_svc

    check_csrf(request, csrf_token)
    assert user.id is not None
    await _require_admin_ui(session, user.id)
    async with session.begin():
        await updates_svc.refresh_all(session)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/update-server")
async def settings_update_server(
    request: Request,
    session: SessionDep,
    user: UiUser,
    csrf_token: Annotated[str, Form()],
    target_version: Annotated[str, Form()],
) -> RedirectResponse:
    from sum_server.updates import system as system_svc

    check_csrf(request, csrf_token)
    assert user.id is not None
    await _require_admin_ui(session, user.id)
    async with session.begin():
        row = await system_svc.request_server_update(
            session, target_version=target_version, actor=user
        )
        row_id = row.id
    # See updates/routes.py: a failed launch must not strand a non-terminal row.
    # The UI reports it by marking the row and redirecting, so the Settings
    # status panel shows the failure instead of an error page.
    try:
        await system_svc.launch_updater()
    except Exception as exc:
        await system_svc.fail_update_by_id(session, row_id, f"launch failed: {exc}")
    return RedirectResponse("/settings", status_code=303)


@router.get("/settings/update-status", response_class=HTMLResponse)
async def settings_update_status(
    request: Request, session: SessionDep, user: UiUser
) -> HTMLResponse:
    """HTMX fragment: current server-update status (polled during an update)."""
    from sum_server.updates import system as system_svc
    from sum_server.updates.models import is_terminal

    assert user.id is not None
    await _require_admin_ui(session, user.id)
    latest = await system_svc.latest_update(session)
    return templates.TemplateResponse(
        request,
        "_update_status.html",
        {
            "server_update": latest,
            "server_update_in_progress": latest is not None and not is_terminal(latest.status),
        },
    )
