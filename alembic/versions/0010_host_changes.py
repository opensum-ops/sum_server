"""host_changes: per-field history of observed and assigned values

Facts and components are stored as current state and overwritten in place, so
nothing recorded what a value used to be. This table keeps the before and after
of every change so the UI can answer "when did this last move".

Backfills one ``add`` row per existing fact and component at its ``first_seen``,
so timelines are populated from real observed data rather than starting empty.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "host_changes",
        sa.Column("host_id", sa.Uuid(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("component_kind", sa.String(length=16), nullable=True),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        sa.Column("subject_label", sa.String(length=128), nullable=True),
        sa.Column("field", sa.String(length=128), nullable=False),
        sa.Column("change", sa.String(length=8), nullable=False),
        sa.Column("old_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("new_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["host_id"],
            ["hosts.id"],
            name=op.f("fk_host_changes_host_id_hosts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_host_changes")),
    )
    op.create_index("ix_host_changes_host_ts", "host_changes", ["host_id", "observed_at"])
    op.create_index("ix_host_changes_field", "host_changes", ["host_id", "scope", "field"])
    op.create_index("ix_host_changes_subject", "host_changes", ["host_id", "subject_id"])

    # Backfill. `first_seen` is when the agent actually first reported the
    # thing, so these rows are observed history, not invented.
    op.execute(
        """
        INSERT INTO host_changes (
            id, host_id, observed_at, scope, component_kind, subject_id,
            subject_label, field, change, old_value, new_value, actor_kind, actor_id
        )
        SELECT gen_random_uuid(), f.host_id, f.first_seen, 'fact', NULL, NULL,
               NULL, f.key, 'add', NULL, f.value, 'agent', NULL
        FROM host_facts f
        """
    )
    op.execute(
        """
        INSERT INTO host_changes (
            id, host_id, observed_at, scope, component_kind, subject_id,
            subject_label, field, change, old_value, new_value, actor_kind, actor_id
        )
        SELECT gen_random_uuid(), c.host_id, c.first_seen, 'component', c.kind, c.id,
               COALESCE(c.slot, c.serial, c.model, c.kind), 'component', 'add', NULL,
               jsonb_build_object(
                   'vendor', c.vendor, 'model', c.model,
                   'serial', c.serial, 'slot', c.slot
               ),
               'agent', NULL
        FROM components c
        """
    )


def downgrade() -> None:
    op.drop_index("ix_host_changes_subject", table_name="host_changes")
    op.drop_index("ix_host_changes_field", table_name="host_changes")
    op.drop_index("ix_host_changes_host_ts", table_name="host_changes")
    op.drop_table("host_changes")
