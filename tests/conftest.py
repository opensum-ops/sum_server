"""Test harness: ephemeral Postgres + FastAPI app + async client + factories."""

from __future__ import annotations

import asyncio
import base64
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey


def _ed25519_inline_seed() -> str:
    seed = SigningKey.generate().encode()
    return "inline:" + base64.b64encode(seed).decode()


@pytest.fixture(scope="session", autouse=True)
def _testcontainer_postgres() -> Iterator[str]:
    """Start Postgres for the test session and configure env."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        url = pg.get_connection_url()
        if "+asyncpg" not in url:
            url = url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
            url = url.replace("postgresql://", "postgresql+asyncpg://")
        os.environ["SUM_SERVER_DATABASE_URL"] = url
        os.environ["SUM_SERVER_SIGNING_PRIVATE_KEY"] = _ed25519_inline_seed()
        os.environ["SUM_SERVER_ENV"] = "test"
        os.environ["SUM_SERVER_LOG_FORMAT"] = "console"
        os.environ["SUM_SERVER_LOG_LEVEL"] = "warning"
        yield url


@pytest.fixture(scope="session", autouse=True)
def _bootstrap_schema(_testcontainer_postgres: str) -> None:
    """Create the schema once per session using ``Base.metadata.create_all``."""
    from sum_server.settings import get_settings

    get_settings.cache_clear()
    settings = get_settings()

    # Import every model so metadata is fully populated.
    import sum_server.agents.models
    import sum_server.auth.models
    import sum_server.components.models
    import sum_server.core.audit
    import sum_server.groups.models
    import sum_server.hosts.models
    import sum_server.teams.models
    import sum_server.updates.models
    import sum_server.users.models  # noqa: F401
    from sum_server.core.db import Base, init_engine

    engine = init_engine(settings.database_url)

    async def _create() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())


@pytest_asyncio.fixture
async def app() -> AsyncIterator[Any]:
    from sum_server.main import create_app

    application = create_app()
    async with application.router.lifespan_context(application):
        yield application


@pytest_asyncio.fixture
async def client(app: Any) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[Any]:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from sum_server.core.db import get_engine

    sm = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with sm() as session:
        yield session


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables() -> AsyncIterator[None]:
    """Truncate state between tests so they remain isolated."""
    yield
    from sum_server.core.db import get_engine

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "TRUNCATE TABLE audit_entries, components, host_facts, "
            "host_groups, group_parameters, host_parameters, groups, "
            "release_cache, system_settings, "
            "agent_enrollments, agent_tokens, session_tokens, "
            "host_owner_teams, host_owner_users, hosts, "
            "team_memberships, teams, users RESTART IDENTITY CASCADE"
        )


# --- Factories --------------------------------------------------------------


async def _create_user(
    db_session: Any, *, email: str, password: str, is_admin: bool = False
) -> Any:
    from sum_server.core.ids import new_id
    from sum_server.core.security.passwords import hash_password
    from sum_server.users.models import User

    user = User(
        id=new_id(),
        email=email.lower(),
        display_name=email.split("@")[0],
        password_hash=hash_password(password),
        is_admin=is_admin,
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def admin_user(db_session: Any) -> Any:
    return await _create_user(
        db_session, email="admin@example.com", password="admin-pw-1234", is_admin=True
    )


@pytest_asyncio.fixture
async def regular_user(db_session: Any) -> Any:
    return await _create_user(db_session, email="user@example.com", password="user-pw-1234")


async def _login(client: AsyncClient, email: str, password: str) -> str:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest_asyncio.fixture
async def admin_token(client: AsyncClient, admin_user: Any) -> str:
    return await _login(client, "admin@example.com", "admin-pw-1234")


@pytest_asyncio.fixture
async def user_token(client: AsyncClient, regular_user: Any) -> str:
    return await _login(client, "user@example.com", "user-pw-1234")


def auth_h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
