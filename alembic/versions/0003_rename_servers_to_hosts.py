"""rename servers to hosts (terminology: a managed machine is a Host)

Pure renames: tables, columns, constraints, indexes. No shape changes.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-23
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (old, new) pairs, applied in order for upgrade and reversed for downgrade.
_TABLES = [
    ("servers", "hosts"),
    ("server_owner_users", "host_owner_users"),
    ("server_owner_teams", "host_owner_teams"),
]

# (table-after-upgrade-rename, old_col, new_col)
_COLUMNS = [
    ("agent_enrollments", "server_id", "host_id"),
    ("agent_tokens", "server_id", "host_id"),
    ("components", "server_id", "host_id"),
    ("host_owner_users", "server_id", "host_id"),
    ("host_owner_teams", "server_id", "host_id"),
]

# (table-after-upgrade-rename, old_constraint, new_constraint)
_CONSTRAINTS = [
    ("hosts", "pk_servers", "pk_hosts"),
    (
        "agent_enrollments",
        "fk_agent_enrollments_server_id_servers",
        "fk_agent_enrollments_host_id_hosts",
    ),
    ("agent_tokens", "fk_agent_tokens_server_id_servers", "fk_agent_tokens_host_id_hosts"),
    ("components", "fk_components_server_id_servers", "fk_components_host_id_hosts"),
    ("host_owner_users", "pk_server_owner_users", "pk_host_owner_users"),
    (
        "host_owner_users",
        "fk_server_owner_users_server_id_servers",
        "fk_host_owner_users_host_id_hosts",
    ),
    (
        "host_owner_users",
        "fk_server_owner_users_user_id_users",
        "fk_host_owner_users_user_id_users",
    ),
    ("host_owner_teams", "pk_server_owner_teams", "pk_host_owner_teams"),
    (
        "host_owner_teams",
        "fk_server_owner_teams_server_id_servers",
        "fk_host_owner_teams_host_id_hosts",
    ),
    (
        "host_owner_teams",
        "fk_server_owner_teams_team_id_teams",
        "fk_host_owner_teams_team_id_teams",
    ),
]

_INDEXES = [
    ("ix_servers_status", "ix_hosts_status"),
    ("ix_agent_tokens_server", "ix_agent_tokens_host"),
    ("ix_components_server_kind", "ix_components_host_kind"),
    ("uq_components_server_kind_serial", "uq_components_host_kind_serial"),
    ("uq_components_server_kind_slot", "uq_components_host_kind_slot"),
    ("ix_server_owner_users_user", "ix_host_owner_users_user"),
    ("ix_server_owner_teams_team", "ix_host_owner_teams_team"),
]


def upgrade() -> None:
    for old, new in _TABLES:
        op.rename_table(old, new)
    for table, old, new in _COLUMNS:
        op.alter_column(table, old, new_column_name=new)
    for table, old, new in _CONSTRAINTS:
        op.execute(f'ALTER TABLE {table} RENAME CONSTRAINT "{old}" TO "{new}"')
    for old, new in _INDEXES:
        op.execute(f'ALTER INDEX "{old}" RENAME TO "{new}"')


def downgrade() -> None:
    for old, new in reversed(_INDEXES):
        op.execute(f'ALTER INDEX "{new}" RENAME TO "{old}"')
    for table, old, new in reversed(_CONSTRAINTS):
        op.execute(f'ALTER TABLE {table} RENAME CONSTRAINT "{new}" TO "{old}"')
    for table, old, new in reversed(_COLUMNS):
        op.alter_column(table, new, new_column_name=old)
    for old, new in reversed(_TABLES):
        op.rename_table(new, old)
