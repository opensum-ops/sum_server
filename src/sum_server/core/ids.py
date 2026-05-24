"""UUID7 generation for sortable primary keys."""
from __future__ import annotations

import uuid

from uuid_utils import uuid7 as _uuid7


def new_id() -> uuid.UUID:
    """Return a UUID7 as a stdlib :class:`uuid.UUID`."""
    return uuid.UUID(bytes=_uuid7().bytes)