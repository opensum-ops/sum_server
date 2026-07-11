"""User services: CRUD with last-admin guard, soft delete, password-rotation rules."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sum_server.core.audit import write_audit
from sum_server.core.errors import ConflictError, NotFoundError
from sum_server.core.ids import new_id
from sum_server.core.pagination import Cursor
from sum_server.core.security.passwords import hash_password
from sum_server.users.models import User
from sum_server.users.schemas import UserCreate, UserUpdate


def _utcnow() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def _normalize_email(email: str) -> str:
    return email.lower().strip()


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    return (
        await session.execute(select(User).where(User.email == _normalize_email(email)))
    ).scalar_one_or_none()


async def list_users(
    session: AsyncSession,
    *,
    limit: int,
    cursor: Cursor | None,
    include_deleted: bool = False,
) -> list[User]:
    """Return at most ``limit + 1`` users so callers can detect a next page."""
    stmt = select(User)
    if not include_deleted:
        stmt = stmt.where(User.deleted_at.is_(None))
    if cursor is not None:
        stmt = stmt.where(
            (User.created_at, User.id) < (cursor.ts, cursor.id)  # type: ignore[operator]
        )
    stmt = stmt.order_by(User.created_at.desc(), User.id.desc()).limit(limit + 1)
    return list((await session.execute(stmt)).scalars().all())


async def count_active_admins(session: AsyncSession) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(User)
                .where(User.is_admin.is_(True), User.deleted_at.is_(None))
            )
        ).scalar_one()
    )


async def create_user(session: AsyncSession, payload: UserCreate) -> User:
    email = _normalize_email(payload.email)
    existing = await get_user_by_email(session, email)
    if existing is not None:
        raise ConflictError("a user with that email already exists")
    user = User(
        id=new_id(),
        email=email,
        display_name=payload.display_name,
        password_hash=hash_password(payload.password),
        is_admin=payload.is_admin,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError("a user with that email already exists") from exc
    await write_audit(
        session,
        action="user.create",
        target_kind="user",
        target_id=user.id,
        payload={"email": user.email, "is_admin": user.is_admin},
    )
    return user


async def update_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    payload: UserUpdate,
    by_admin: bool,
    if_match_version: int | None = None,
) -> User:
    """Update fields on a user.

    Non-admin callers may only touch their own ``display_name``, ``email``,
    ``password`` (routes enforce caller == user_id). ``is_admin`` is admin-only.
    """
    user = await get_user(session, user_id)
    if user is None or user.deleted_at is not None:
        raise NotFoundError("user not found")
    if if_match_version is not None and user.version != if_match_version:
        from sum_server.core.errors import PreconditionFailedError

        raise PreconditionFailedError("user has been modified")

    changed: dict[str, object] = {}
    if payload.display_name is not None and payload.display_name != user.display_name:
        user.display_name = payload.display_name

        changed["display_name"] = payload.display_name
    if payload.email is not None:
        new_email = _normalize_email(payload.email)
        if new_email != user.email:
            clash = await get_user_by_email(session, new_email)

            if clash is not None and clash.id != user.id:
                raise ConflictError("another user already uses that email")

            user.email = new_email
            changed["email"] = new_email
    if payload.password is not None:
        user.password_hash = hash_password(payload.password)
        changed["password"] = "***"  # noqa: S105  redaction marker, not a credential

    if payload.is_admin is not None and by_admin and payload.is_admin != user.is_admin:
        if not payload.is_admin and user.is_admin:
            # Removing admin: ensure at least one other admin remains.
            others = await count_active_admins(session)

            if others <= 1:
                raise ConflictError("cannot remove the last remaining admin")
        user.is_admin = payload.is_admin

        changed["is_admin"] = payload.is_admin

    if changed:
        await write_audit(
            session,
            action="user.update",
            target_kind="user",
            target_id=user.id,
            payload={"changed": list(changed.keys())},
        )

    return user


async def delete_user(session: AsyncSession, *, user_id: uuid.UUID) -> User:
    """Soft delete: set ``deleted_at`` and revoke active sessions.

    Refuses to delete the last remaining admin
    """
    user = await get_user(session, user_id)
    if user is None or user.deleted_at is not None:
        raise NotFoundError("user not found")
    if user.is_admin and await count_active_admins(session) <= 1:
        raise ConflictError("cannot delete the last remaining admin")

    user.deleted_at = _utcnow()

    # Revoke active sessions.
    from sum_server.auth.service import revoke_all_user_sessions

    await revoke_all_user_sessions(session, user_id=user.id)

    await write_audit(
        session,
        action="user.delete",
        target_kind="user",
        target_id=user.id,
        payload={"email": user.email},
    )
    return user
