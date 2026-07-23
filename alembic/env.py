"""Async-aware Alembic environment."""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Import every model so Base.metadata is fully populated for autogenerate.
from sum_server.agents.models import AgentEnrollment  # noqa: F401
from sum_server.auth.models import AgentToken, SessionToken  # noqa: F401
from sum_server.components.models import Component  # noqa: F401
from sum_server.core.audit import AuditEntry  # noqa: F401
from sum_server.core.db import Base
from sum_server.servers.models import (  # noqa: F401
    Server,
    server_owner_teams,
    server_owner_users,
)
from sum_server.settings import get_settings
from sum_server.teams.models import Team, TeamMembership  # noqa: F401
from sum_server.users.models import User  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
