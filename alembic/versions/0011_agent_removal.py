"""hosts.agent_removal_requested_at: pending "remove the agent" intent

The server cannot reach into a host to uninstall anything, so removal is
desired state: this column records that an operator asked, and the agent
collects a signed directive on its next heartbeat.

A timestamp rather than a boolean so the UI can say how long a request has been
waiting on a host that is not checking in, and so the directive can be bound to
one request rather than being replayable forever.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "hosts",
        sa.Column("agent_removal_requested_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("hosts", "agent_removal_requested_at")
