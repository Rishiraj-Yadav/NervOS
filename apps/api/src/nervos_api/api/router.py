"""Versioned API router composition."""

from fastapi import APIRouter

from nervos_api.api.routes.auth import router as auth_router
from nervos_api.api.routes.health import router as health_router
from nervos_api.api.routes.setup import router as setup_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health_router)
api_router.include_router(setup_router)
api_router.include_router(auth_router)
