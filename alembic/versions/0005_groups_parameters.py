"""groups tree + host memberships + parameters; seed the global root group

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-23
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "groups",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=1024), nullable=True),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["groups.id"],
            name=op.f("fk_groups_parent_id_groups"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_groups")),
        sa.UniqueConstraint("name", name=op.f("uq_groups_name")),
    )
    op.create_table(
        "host_groups",
        sa.Column("host_id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["groups.id"],
            name=op.f("fk_host_groups_group_id_groups"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["host_id"],
            ["hosts.id"],
            name=op.f("fk_host_groups_host_id_hosts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("host_id", "group_id", name=op.f("pk_host_groups")),
    )
    op.create_index("ix_host_groups_group", "host_groups", ["group_id"], unique=False)
    op.create_table(
        "group_parameters",
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["groups.id"],
            name=op.f("fk_group_parameters_group_id_groups"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_group_parameters")),
        sa.UniqueConstraint("group_id", "key", name=op.f("uq_group_parameters_group_id_key")),
    )
    op.create_table(
        "host_parameters",
        sa.Column("host_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["host_id"],
            ["hosts.id"],
            name=op.f("fk_host_parameters_host_id_hosts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_host_parameters")),
        sa.UniqueConstraint("host_id", "key", name=op.f("uq_host_parameters_host_id_key")),
    )

    # Seed the protected root group (the app lifespan also self-heals this).
    op.execute(
        sa.text(
            "INSERT INTO groups (id, name, description, parent_id, created_at, updated_at) "
            "VALUES (:id, 'global', 'Implicit root group; every host is a member.', "
            "NULL, now(), now())"
        ).bindparams(id=uuid.uuid4())
    )


def downgrade() -> None:
    op.drop_table("host_parameters")
    op.drop_table("group_parameters")
    op.drop_index("ix_host_groups_group", table_name="host_groups")
    op.drop_table("host_groups")
    op.drop_table("groups")
