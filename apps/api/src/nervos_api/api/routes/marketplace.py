"""Authenticated local proxy for public Marketplace discovery."""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from nervos_api.api.dependencies import CurrentUserDependency
from nervos_api.application.marketplace_discovery import MarketplaceDiscoveryService
from nervos_api.application.marketplace_installation import MarketplaceInstallService

router = APIRouter(prefix="/marketplace", tags=["marketplace"])


def service(request: Request) -> MarketplaceDiscoveryService:
    return request.app.state.marketplace_discovery


def install_service(request: Request) -> MarketplaceInstallService:
    return cast(MarketplaceInstallService, request.app.state.marketplace_installation)


class InstallRequestBody(BaseModel):
    package_id: str = Field(min_length=3, max_length=128)
    package_version: str = Field(min_length=1, max_length=64)
    acknowledgement: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")


@router.get("/packages")
async def search_packages(
    request: Request,
    q: Annotated[str, Query(max_length=128)] = "",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
) -> dict[str, Any]:
    """Search the configured hosted catalog without forwarding local identity."""
    return await service(request).search(q, limit, cursor)


@router.get("/packages/{package_id}")
async def get_package(request: Request, package_id: str) -> dict[str, Any]:
    return await service(request).package(package_id)


@router.get("/packages/{package_id}/versions")
async def get_versions(
    request: Request,
    package_id: str,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
) -> dict[str, Any]:
    return await service(request).versions(package_id, cursor)


@router.get("/packages/{package_id}/versions/{version}")
async def get_release(request: Request, package_id: str, version: str) -> dict[str, Any]:
    return await service(request).release(package_id, version)


@router.post("/install-requests")
async def create_install_request(
    request: Request, payload: InstallRequestBody, user: CurrentUserDependency
) -> dict[str, Any]:
    settings = request.app.state.settings
    origin = settings.marketplace_origin
    if origin is None:
        from nervos_core.application.errors import PersistenceUnavailable

        raise PersistenceUnavailable
    return await install_service(request).create(
        user.id, origin, payload.package_id, payload.package_version, payload.acknowledgement
    )


@router.get("/install-requests/{request_id}")
def get_install_request(
    request: Request, request_id: int, user: CurrentUserDependency
) -> dict[str, Any]:
    return install_service(request).status(user.id, request_id)


@router.post("/install-requests/{request_id}/install")
async def install_request(
    request: Request, request_id: int, user: CurrentUserDependency
) -> dict[str, Any]:
    return await install_service(request).install(user.id, request_id)


@router.post("/install-requests/{request_id}/prepare")
async def prepare_request(
    request: Request, request_id: int, user: CurrentUserDependency
) -> dict[str, Any]:
    return await install_service(request).prepare(user.id, request_id)


@router.post("/install-requests/{request_id}/cancel")
def cancel_request(
    request: Request, request_id: int, user: CurrentUserDependency
) -> dict[str, Any]:
    return install_service(request).cancel(user.id, request_id)
