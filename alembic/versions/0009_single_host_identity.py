"""collapse host name and hostname into one identity field

A managed machine has one name. ``name`` held the operator's enrollment label
and ``hostname`` the agent's observed value, so every read site had to render
``hostname or name`` and the two could disagree indefinitely. ``hostname`` wins:
enrollment seeds it with the operator's label and the agent overwrites it on
first inventory.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ``name`` is NOT NULL today, so no row can come out of this without a
    # value. Hosts that never reported keep their enrollment label.
    op.execute("UPDATE hosts SET hostname = name WHERE hostname IS NULL")
    op.alter_column("hosts", "hostname", existing_type=sa.String(length=256), nullable=False)
    op.drop_column("hosts", "name")


def downgrade() -> None:
    op.add_column("hosts", sa.Column("name", sa.String(length=256), nullable=True))
    op.execute("UPDATE hosts SET name = hostname")
    op.alter_column("hosts", "name", existing_type=sa.String(length=256), nullable=False)
    op.alter_column("hosts", "hostname", existing_type=sa.String(length=256), nullable=True)
