"""FastAPI app factory + lifespan.

Starts the async engine, loads the Ed25519 signing key, runs a bootstrap admin
seed (idempotent), and kicks off a periodic expired-jobs sweeper. Mounts the
``/api/v1`` router plus health/ready/well-known endpoints.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import quote

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from sum_server import __version__
from sum_server.api.v1 import api_v1
from sum_server.core.db import dispose_engine, get_engine, get_session, init_engine
from sum_server.core.errors import install_error_handlers
from sum_server.core.logging import RequestIdMiddleware, configure_logging
from sum_server.core.security import signing
from sum_server.jobs import service as jobs_svc
from sum_server.settings import get_settings
from sum_server.ui.deps import LoginRequiredError
from sum_server.ui.routes import router as ui_router

log = structlog.get_logger(__name__)


async def _expiry_sweep_loop(stop: asyncio.Event, interval_seconds: int = 30) -> None:
    """Periodically flip pending jobs past ``expires_at`` to ``expired``."""
    sm = async_sessionmaker(get_engine(), expire_on_commit=False)
    while not stop.is_set():
        try:
            async with sm() as session, session.begin():
                n = await jobs_svc.sweep_expired(session)
                if n:
                    log.info("expired_jobs_swept", count=n)
        except Exception:
            log.exception("expiry_sweep_failed")
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)


async def _bootstrap_admin() -> None:
    """Idempotently create the configured bootstrap admin user (no-op if absent)."""
    settings = get_settings()
    if not settings.bootstrap_admin_email or not settings.bootstrap_admin_password:
        return
    from sqlalchemy import select

    from sum_server.core.security.passwords import hash_password
    from sum_server.users.models import User

    sm = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with sm() as session, session.begin():
        email = settings.bootstrap_admin_email.lower().strip()
        existing = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
        if existing is not None:
            return
        admin = User(
            email=email,
            display_name="Bootstrap Admin",
            password_hash=hash_password(settings.bootstrap_admin_password),
            is_admin=True,
        )
        session.add(admin)
        log.info("bootstrap_admin_created", email=email)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings)
    log.info("starting", env=settings.env.value)
    init_engine(settings.database_url)
    signing.load_signing_key(settings.signing_private_key)
    await _bootstrap_admin()

    stop = asyncio.Event()
    sweep_task = asyncio.create_task(_expiry_sweep_loop(stop))
    try:
        yield
    finally:
        stop.set()
        await asyncio.gather(sweep_task, return_exceptions=True)
        await dispose_engine()
        log.info("stopped")


def create_app() -> FastAPI:
    app = FastAPI(title="sum_server", version=__version__, lifespan=lifespan)
    app.add_middleware(RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(api_v1)

    app.include_router(ui_router)
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent / "ui" / "static")),
        name="static",
    )

    @app.exception_handler(LoginRequiredError)
    async def login_redirect(_request: Request, exc: LoginRequiredError) -> RedirectResponse:
        return RedirectResponse(f"/login?next={quote(exc.next_path)}", status_code=303)

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", tags=["meta"])
    async def readyz() -> JSONResponse:
        reasons: list[str] = []
        if not signing.is_loaded():
            reasons.append("signing key not loaded")
        try:
            async for session in get_session():
                await session.execute(text("SELECT 1"))
                break
        except Exception as exc:
            reasons.append(f"db unreachable: {exc.__class__.__name__}")
        if reasons:
            return JSONResponse(
                status_code=503, content={"status": "not_ready", "reasons": reasons}
            )
        return JSONResponse(content={"status": "ready"})

    @app.get("/.well-known/sum-server-signing-key", tags=["meta"])
    async def signing_key() -> dict[str, str]:
        return {
            "algorithm": "ed25519",
            "public_key_b64": signing.get_public_key_b64(),
            "fetched_at": dt.datetime.now(tz=dt.UTC).isoformat(),
        }

    return app


app = create_app()
