"""The change-history row."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sum_server.core.db import Base, IdMixin

# What kind of thing changed. Drives which pane a change belongs to.
#
# ``group`` and ``param`` are the human-assigned side and only ever cover what
# is host-scoped: this host joining or leaving a group, and this host's own
# parameter overrides. A parameter set on a *group* changes an effective value
# for every member and is deliberately not fanned out into a row per host; it
# is a property of the group, is already audited, and the effective-parameters
# table names its source.
CHANGE_SCOPES = ("host", "fact", "component", "group", "param")

# ``add`` and ``del`` disambiguate SQL NULL from a JSON ``null`` value: on an
# add the old value is meaningless, on a delete the new one is.
CHANGE_KINDS = ("add", "edit", "del")


class HostChange(Base, IdMixin):
    __tablename__ = "host_changes"

    host_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("hosts.id", ondelete="CASCADE"), nullable=False
    )
    observed_at: Mapped[dt.datetime] = mapped_column(
        default=lambda: dt.datetime.now(tz=dt.UTC), nullable=False
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False)

    # Component-scoped changes only. ``subject_label`` is a snapshot taken at
    # write time so a timeline survives its component row being replaced.
    component_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    subject_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    subject_label: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # "hostname", a fact key, "model", "attrs.size_bytes", "present", ...
    field: Mapped[str] = mapped_column(String(128), nullable=False)
    change: Mapped[str] = mapped_column(String(8), nullable=False)
    old_value: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[Any | None] = mapped_column(JSONB, nullable=True)

    # Who observed it: an agent ingest, or a human editing through the UI.
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    __table_args__ = (
        # The pane feed: everything for a host, newest first.
        Index("ix_host_changes_host_ts", "host_id", "observed_at"),
        # The per-field control.
        Index("ix_host_changes_field", "host_id", "scope", "field"),
        # The per-component control.
        Index("ix_host_changes_subject", "host_id", "subject_id"),
    )
