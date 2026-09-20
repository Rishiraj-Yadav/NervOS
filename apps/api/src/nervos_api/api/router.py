"""Versioned API router composition."""

from fastapi import APIRouter

from nervos_api.api.routes.agent_instances import router as agent_instances_router
from nervos_api.api.routes.auth import router as auth_router
from nervos_api.api.routes.health import router as health_router
from nervos_api.api.routes.mcp_connections import router as mcp_connections_router
from nervos_api.api.routes.runs import router as runs_router
from nervos_api.api.routes.setup import router as setup_router
from nervos_api.api.routes.triggers import router as triggers_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health_router)
api_router.include_router(setup_router)
api_router.include_router(auth_router)
api_router.include_router(agent_instances_router)
api_router.include_router(mcp_connections_router)
api_router.include_router(triggers_router)
api_router.include_router(runs_router)
