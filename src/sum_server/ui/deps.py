"""Web UI dependencies: cookie session auth + CSRF (double-submit cookie).

The JSON API authenticates with ``Authorization: Bearer``; the browser UI
uses the same opaque ``SessionToken``, delivered in an HttpOnly cookie
instead. One token model, two transports -- revocation and expiry behave
identically for both.
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Request

from sum_server.core.context import Actor, set_actor
from sum_server.core.db import SessionDep

SESSION_COOKIE = "sum_session"
CSRF_COOKIE = "sum_csrf"
CSRF_HEADER = "x-csrftoken"
CSRF_FIELD = "csrf_token"


class LoginRequiredError(Exception):
    """A UI page needs an authenticated user; handled as a 303 to ``/login``."""

    def __init__(self, next_path: str = "/") -> None:
        self.next_path = next_path


async def current_ui_user(request: Request, session: SessionDep) -> Actor:
    """Resolve the ``sum_session`` cookie to a *user* actor, or bounce to login."""
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        raise LoginRequiredError(request.url.path)

    from sum_server.auth.service import resolve_actor_from_token

    actor = await resolve_actor_from_token(session, raw)
    # Persist the token touch and release the auto-begun read transaction
    # (same discipline as core.deps.current_actor).
    await session.commit()
    if actor is None or actor.kind != "user":
        raise LoginRequiredError(request.url.path)
    set_actor(actor)
    return actor


UiUser = Annotated[Actor, Depends(current_ui_user)]


def new_csrf_token() -> str:
    """Mint a token for the CSRF double-submit cookie."""
    return secrets.token_urlsafe(32)


def check_csrf(request: Request, submitted: str | None = None) -> None:
    """Double-submit check for state-changing UI requests.

    The ``sum_csrf`` cookie must be echoed back by the page -- either in the
    ``X-CSRFToken`` header (HTMX ``hx-headers``) or the ``csrf_token`` hidden
    form field (plain forms, passed as ``submitted``). A cross-site attacker
    can *send* our cookies but cannot *read* them, so it cannot produce the
    echo.
    """
    cookie = request.cookies.get(CSRF_COOKIE)
    echoed = submitted or request.headers.get(CSRF_HEADER)
    if not cookie or not echoed or not secrets.compare_digest(cookie, echoed):
        from sum_server.core.errors import ForbiddenError

        raise ForbiddenError("CSRF check failed")
