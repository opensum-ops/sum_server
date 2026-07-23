"""Authentication services: user login/logout + token resolution.

:func:`resolve_actor_from_token` is the single entry point for ``current_actor``;
it checks user session tokens first, then agent tokens.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sum_server.auth.models import AgentToken, SessionToken
from sum_server.core.audit import write_audit
from sum_server.core.context import Actor
from sum_server.core.errors import AuthError
from sum_server.core.ids import new_id
from sum_server.core.security.passwords import verify_password
from sum_server.core.security.tokens import hash_token, mint_token
from sum_server.settings import get_settings


def _utcnow() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


async def resolve_actor_from_token(session: AsyncSession, raw_token: str) -> Actor | None:
    """Resolve a bearer token to an :class:`Actor`, or ``None`` if invalid/expired."""
    token_hash = hash_token(raw_token)
    now = _utcnow()

    sess = (
        await session.execute(select(SessionToken).where(SessionToken.token_hash == token_hash))
    ).scalar_one_or_none()
    if sess is not None:
        if sess.revoked_at is not None or sess.expires_at <= now:
            return None
        sess.last_used_at = now
        return Actor(kind="user", id=sess.user_id)

    agent = (
        await session.execute(select(AgentToken).where(AgentToken.token_hash == token_hash))
    ).scalar_one_or_none()
    if agent is not None:
        if agent.revoked_at is not None:
            return None
        if agent.expires_at is not None and agent.expires_at <= now:
            return None
        agent.last_seen_at = now
        return Actor(kind="agent", id=agent.host_id)

    return None


async def login(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    ip: str | None,
    user_agent: str | None,
) -> tuple[str, SessionToken]:
    """Verify credentials and mint a session token. Returns ``(raw_token, session_row)``."""
    from sum_server.users.models import User

    email_norm = email.lower().strip()
    user = (
        await session.execute(select(User).where(User.email == email_norm))
    ).scalar_one_or_none()
    if user is None or user.deleted_at is not None:
        raise AuthError("invalid credentials")
    if not verify_password(password, user.password_hash):
        raise AuthError("invalid credentials")

    raw, token_hash = mint_token()
    settings = get_settings()
    expires_at = _utcnow() + dt.timedelta(seconds=settings.session_token_ttl_seconds)
    sess = SessionToken(
        id=new_id(),
        user_id=user.id,
        token_hash=token_hash,
        expires_at=expires_at,
        ip=ip,
        user_agent=user_agent,
    )
    session.add(sess)
    await write_audit(
        session,
        action="auth.login",
        target_kind="user",
        target_id=user.id,
        actor_kind="user",
        actor_id=user.id,
        payload={"ip": ip},
    )
    return raw, sess


async def logout(session: AsyncSession, *, raw_token: str) -> None:
    token_hash = hash_token(raw_token)
    sess = (
        await session.execute(select(SessionToken).where(SessionToken.token_hash == token_hash))
    ).scalar_one_or_none()
    if sess is None or sess.revoked_at is not None:
        return
    sess.revoked_at = _utcnow()
    await write_audit(
        session,
        action="auth.logout",
        target_kind="user",
        target_id=sess.user_id,
    )


async def revoke_all_user_sessions(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    except_session_id: uuid.UUID | None = None,
) -> int:
    """Revoke every active session for ``user_id`` except (optionally) the given one."""
    now = _utcnow()
    rows = (
        (
            await session.execute(
                select(SessionToken).where(
                    SessionToken.user_id == user_id, SessionToken.revoked_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    n = 0
    for s in rows:
        if except_session_id is not None and s.id == except_session_id:
            continue
        s.revoked_at = now
        n += 1
    return n


async def mint_agent_token(
    session: AsyncSession,
    *,
    host_id: uuid.UUID,
    ip: str | None,
) -> tuple[str, AgentToken]:
    """Create a new agent token, revoking any prior active token for the host."""
    existing = (
        (
            await session.execute(
                select(AgentToken).where(
                    AgentToken.host_id == host_id, AgentToken.revoked_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    now = _utcnow()
    for t in existing:
        t.revoked_at = now

    raw, token_hash = mint_token()
    settings = get_settings()
    expires_at: dt.datetime | None = None
    if settings.agent_token_ttl_seconds > 0:
        expires_at = now + dt.timedelta(seconds=settings.agent_token_ttl_seconds)
    tok = AgentToken(
        id=new_id(),
        host_id=host_id,
        token_hash=token_hash,
        expires_at=expires_at,
        ip=ip,
    )
    session.add(tok)
    return raw, tok
