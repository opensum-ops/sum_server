"""Group tree, host memberships, and parameter models.

Groups form a single-parent tree rooted at the protected ``global`` group
(the only row with ``parent_id IS NULL``). Every host is implicitly a member
of ``global``; explicit membership is many-to-many via ``host_groups``.
Parameters are human-assigned key/value pairs on groups and hosts; see
``groups/resolution.py`` for the inheritance rules.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Column, ForeignKey, Index, String, Table, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sum_server.core.db import Base, IdMixin, TimestampMixin

GLOBAL_GROUP_NAME = "global"

host_groups = Table(
    "host_groups",
    Base.metadata,
    Column("host_id", ForeignKey("hosts.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
    Index("ix_host_groups_group", "group_id"),
)


class Group(Base, IdMixin, TimestampMixin):
    __tablename__ = "groups"

    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Only the global root has no parent; RESTRICT so deleting a parent with
    # children fails at the DB level too (the service refuses first).
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("groups.id", ondelete="RESTRICT"), nullable=True
    )


class GroupParameter(Base, IdMixin, TimestampMixin):
    __tablename__ = "group_parameters"

    group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)

    __table_args__ = (UniqueConstraint("group_id", "key"),)


class HostParameter(Base, IdMixin, TimestampMixin):
    __tablename__ = "host_parameters"

    host_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("hosts.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)

    __table_args__ = (UniqueConstraint("host_id", "key"),)
