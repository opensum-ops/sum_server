"""Cursor pagination helpers.

Cursors are opaque base64(json) carrying ``(id, ts)`` so paging is stable across
inserts. All collection routes return ``Page`` with ``items`` and ``next_cursor``.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import uuid
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Query
from pydantic import BaseModel

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


@dataclass(frozen=True, slots=True)
class Cursor:
    id: uuid.UUID
    ts: dt.datetime

    def encode(self) -> str:
        payload = {"id": str(self.id), "ts": self.ts.isoformat()}
        return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()

    @classmethod
    def decode(cls, raw: str) -> Cursor:
        try:
            payload = json.loads(base64.urlsafe_b64decode(raw.encode()).decode())
            return cls(
                id=uuid.UUID(payload["id"]),
                ts=dt.datetime.fromisoformat(payload["ts"]),
            )
        except Exception as exc:
            raise ValueError(f"invalid cursor: {exc}") from exc


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None


def page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
) -> tuple[int, Cursor | None]:
    parsed = Cursor.decode(cursor) if cursor else None
    return limit, parsed


PageParams = Annotated[tuple[int, Cursor | None], "from page_params dep"]


def build_next_cursor(items: list[Any], *, ts_attr: str = "created_at") -> str | None:
    """Given the items just returned, build the cursor pointing to the next page.

    Assumes the items are sorted by ``(ts_attr DESC, id DESC)``. Returns ``None`` when
    there is no further page (caller decides based on whether ``len(items) ==
    limit + 1``; this helper just encodes the last item).
    """
    if not items:
        return None
    last = items[-1]
    return Cursor(id=last.id, ts=getattr(last, ts_attr)).encode()
