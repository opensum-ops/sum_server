"""drop jobs and job_results (job execution removed; inventory refocus)

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("job_results")
    op.drop_index("ix_jobs_server_status", table_name="jobs")
    op.drop_index("ix_jobs_expires", table_name="jobs")
    op.drop_table("jobs")


def downgrade() -> None:
    # Mirrors the 0001 DDL; restores empty tables only (data is not recoverable).
    op.create_table(
        "jobs",
        sa.Column("server_id", sa.Uuid(), nullable=False),
        sa.Column("capability", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("nonce", sa.LargeBinary(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("signature", sa.LargeBinary(length=64), nullable=False),
        sa.Column("picked_up_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_actor_kind", sa.String(length=16), nullable=False),
        sa.Column("created_by_actor_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["server_id"],
            ["servers.id"],
            name=op.f("fk_jobs_server_id_servers"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index("ix_jobs_expires", "jobs", ["expires_at"], unique=False)
    op.create_index("ix_jobs_server_status", "jobs", ["server_id", "status"], unique=False)
    op.create_table(
        "job_results",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_job_results_job_id_jobs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("job_id", name=op.f("pk_job_results")),
    )
