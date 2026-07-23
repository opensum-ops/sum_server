"""Aggregator: mount all v1 routers under ``/api/v1``."""

from __future__ import annotations

from fastapi import APIRouter

from sum_server.agents.routes import router as agents_router
from sum_server.audit.routes import router as audit_router
from sum_server.auth.routes import router as auth_router
from sum_server.components.routes import router as components_router
from sum_server.servers.routes import router as servers_router
from sum_server.teams.routes import router as teams_router
from sum_server.users.routes import router as users_router

api_v1 = APIRouter(prefix="/api/v1")
api_v1.include_router(auth_router)
api_v1.include_router(users_router)
api_v1.include_router(teams_router)
api_v1.include_router(servers_router)
api_v1.include_router(components_router)
api_v1.include_router(agents_router)
api_v1.include_router(audit_router)
