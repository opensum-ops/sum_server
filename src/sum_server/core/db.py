"""Async SQLAlchemy engine, session factory, declarative ``Base``, and FastAPI dep."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any, ClassVar

from fastapi import Depends
from sqlalchemy import DateTime, MetaData
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import NullPool

from sum_server.core.ids import new_id
from sum_server.settings import Env, get_settings

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[type, Any]] = {dt.datetime: DateTime(timezone=True)}


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(
        default=lambda: dt.datetime.now(tz=dt.UTC), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        default=lambda: dt.datetime.now(tz=dt.UTC),
        onupdate=lambda: dt.datetime.now(tz=dt.UTC),
        nullable=False,
    )


class IdMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_engine(database_url: str | None = None) -> AsyncEngine:
    """Create the async engine. Idempotent."""
    global _engine, _sessionmaker
    if _engine is not None:
        return _engine
    url = database_url or get_settings().database_url
    if get_settings().env is Env.test:
        # NullPool: no pooled asyncpg connections to outlive per-test event loops.
        _engine = create_async_engine(url, poolclass=NullPool, future=True)
    else:
        _engine = create_async_engine(
            url,
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
            future=True,
        )
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        return init_engine()
    return _engine


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def get_session() -> AsyncIterator[AsyncSession]:
    if _sessionmaker is None:
        init_engine()
    assert _sessionmaker is not None
    async with _sessionmaker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


SessionDep = Annotated[AsyncSession, Depends(get_session)]
