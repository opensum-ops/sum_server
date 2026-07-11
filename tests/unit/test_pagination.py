from __future__ import annotations

import datetime as dt
import uuid

import pytest

from sum_server.core.pagination import Cursor


def test_cursor_round_trip() -> None:
    c = Cursor(id=uuid.uuid4(), ts=dt.datetime.now(tz=dt.UTC))
    encoded = c.encode()
    decoded = Cursor.decode(encoded)
    assert decoded.id == c.id
    assert decoded.ts == c.ts


def test_cursor_decode_invalid_raises() -> None:
    with pytest.raises(ValueError):
        Cursor.decode("not-a-cursor")
