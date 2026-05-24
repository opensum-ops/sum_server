"""Shared FastAPI dependencies (current actor, role gates).

Avoids circular imports by deferring imports of domain modules to inside the
dependency functions.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header

from sum_server.core.context import Actor, set_actor
from sum_server.core.db import SessionDep
from sum_server.core.errors import AuthError, ForbiddenError


async def current_actor(
    session: SessionDep, authorization: Annotated[str | None, Header()] = None
) -> Actor:
    """Resolve the ``Actor`` from the ``Authorization: Bearer ...`` header.

    Tries user session tokens first, then agent tokens. Sets the actor
    contextvar so audit writes can attribute correctly. Raises ``AuthError``
    when missing or invalid.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthError("missing bearer token")
    token = authorization.split(" ", 1)[1].strip()

    # Local import: auth.service depends on core, not vice versa.
    from sum_server.auth.service import resolve_actor_from_token

    actor = await resolve_actor_from_token(session, token)
    if actor is None:
        raise AuthError("invalid or expired token")
    set_actor(actor)
    return actor


CurrentActor = Annotated[Actor, Depends(current_actor)]


async def require_user(actor: CurrentActor) -> Actor:
    if actor.kind != "user":
        raise ForbiddenError("user-only endpoint")
    return actor


async def require_agent(actor: CurrentActor) -> Actor:
    if actor.kind != "agent":
        raise ForbiddenError("agent-only endpoint")
    return actor


async def require_admin(actor: CurrentActor, session: SessionDep) -> Actor:
    """Require a user actor whose backing user has ``is_admin=True``."""
    if actor.kind != "user" or actor.id is None:
        raise ForbiddenError("admin-only endpoint")
    from sum_server.users.service import get_user

    user = await get_user(session, actor.id)
    if user is None or not user.is_admin:
        raise ForbiddenError("admin-only endpoint")
    return actor


UserActor = Annotated[Actor, Depends(require_user)]
AgentActor = Annotated[Actor, Depends(require_agent)]
AdminActor = Annotated[Actor, Depends(require_admin)]
