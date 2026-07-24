"""Async status reporter backed by the ``server_updates`` row.

Each transition is its own committed transaction so the row survives the
sum-server restart the updater triggers and the UI can poll it live.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sum_server.updates.models import ServerUpdate, is_terminal


class DbReporter:
    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], update_id: uuid.UUID
    ) -> None:
        self._sm = sessionmaker
        self._id = update_id

    async def set(self, status: str, detail: str | None = None) -> None:
        now = dt.datetime.now(tz=dt.UTC)
        values: dict[str, object] = {"status": status, "detail": detail}
        if is_terminal(status):
            values["finished_at"] = now
        async with self._sm() as session, session.begin():
            await session.execute(
                update(ServerUpdate).where(ServerUpdate.id == self._id).values(**values)
            )

    async def set_dump_path(self, path: str) -> None:
        async with self._sm() as session, session.begin():
            await session.execute(
                update(ServerUpdate).where(ServerUpdate.id == self._id).values(dump_path=path)
            )
