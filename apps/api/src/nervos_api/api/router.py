"""Versioned API router composition."""

from fastapi import APIRouter

from nervos_api.api.routes.agent_instances import router as agent_instances_router
from nervos_api.api.routes.auth import router as auth_router
from nervos_api.api.routes.conversations import router as conversations_router
from nervos_api.api.routes.health import router as health_router
from nervos_api.api.routes.marketplace import router as marketplace_router
from nervos_api.api.routes.mcp_connections import router as mcp_connections_router
from nervos_api.api.routes.memories import router as memories_router
from nervos_api.api.routes.packages import router as packages_router
from nervos_api.api.routes.runs import router as runs_router
from nervos_api.api.routes.runtime_integration import router as runtime_integration_router
from nervos_api.api.routes.secrets import router as secrets_router
from nervos_api.api.routes.security import router as security_router
from nervos_api.api.routes.setup import router as setup_router
from nervos_api.api.routes.triggers import router as triggers_router
from nervos_api.api.routes.workflows import router as workflows_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health_router)
api_router.include_router(setup_router)
api_router.include_router(auth_router)
api_router.include_router(packages_router)
api_router.include_router(agent_instances_router)
api_router.include_router(mcp_connections_router)
api_router.include_router(triggers_router)
api_router.include_router(runs_router)
api_router.include_router(conversations_router)
api_router.include_router(memories_router, prefix="/memories")
api_router.include_router(marketplace_router)
api_router.include_router(runtime_integration_router)
api_router.include_router(workflows_router)
# Stage H: the secret manager plus the H2/H3/H5 owner-scoped resources it protects. These are
# control-plane configuration and decisions only -- no route here executes anything.
api_router.include_router(secrets_router)
api_router.include_router(security_router)
