"""Aggregator: mount all v1 routers under ``/api/v1``."""

from __future__ import annotations

from fastapi import APIRouter

from sum_server.agents.routes import router as agents_router
from sum_server.audit.routes import router as audit_router
from sum_server.auth.routes import router as auth_router
from sum_server.components.routes import router as components_router
from sum_server.groups.routes import router as groups_router
from sum_server.hosts.routes import router as hosts_router
from sum_server.teams.routes import router as teams_router
from sum_server.updates.routes import router as updates_router
from sum_server.updates.routes import system_router
from sum_server.users.routes import router as users_router

api_v1 = APIRouter(prefix="/api/v1")
api_v1.include_router(auth_router)
api_v1.include_router(users_router)
api_v1.include_router(teams_router)
api_v1.include_router(hosts_router)
api_v1.include_router(groups_router)
api_v1.include_router(components_router)
api_v1.include_router(agents_router)
api_v1.include_router(updates_router)
api_v1.include_router(system_router)
api_v1.include_router(audit_router)
