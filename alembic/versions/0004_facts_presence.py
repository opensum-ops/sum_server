"""host facts + presence columns

Adds the ``host_facts`` table (agent-observed key/value facts) and the
presence signal columns on ``hosts`` (last_heartbeat_at, reported_presence,
boot_id). Presence itself is derived at read time and never stored.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "hosts", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("hosts", sa.Column("reported_presence", sa.String(length=16), nullable=True))
    op.add_column("hosts", sa.Column("boot_id", sa.String(length=64), nullable=True))
    op.create_table(
        "host_facts",
        sa.Column("host_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["host_id"],
            ["hosts.id"],
            name=op.f("fk_host_facts_host_id_hosts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_host_facts")),
        sa.UniqueConstraint("host_id", "key", name=op.f("uq_host_facts_host_id_key")),
    )
    op.create_index("ix_host_facts_key", "host_facts", ["key"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_host_facts_key", table_name="host_facts")
    op.drop_table("host_facts")
    op.drop_column("hosts", "boot_id")
    op.drop_column("hosts", "reported_presence")
    op.drop_column("hosts", "last_heartbeat_at")
