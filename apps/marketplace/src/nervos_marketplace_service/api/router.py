"""Thin public read routes. No publisher/admin/write surface."""

import base64
from typing import Annotated, cast

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.types import Receive, Scope, Send

from nervos_marketplace_service.api.cursors import decode, encode, fingerprint
from nervos_marketplace_service.application.artifact_reads import ArtifactReadService
from nervos_marketplace_service.application.catalog_queries import (
    MarketplaceCatalogQueryService,
    bounds,
)
from nervos_marketplace_service.application.ports import DependencyReadiness, VerifiedArtifactStream
from nervos_marketplace_service.domain.catalog import (
    CursorPage,
    PackageDetail,
    PackageReleaseDetail,
    PackageSummary,
    PackageVersionSummary,
)

router = APIRouter()
Limit = Annotated[int, Query(ge=1, le=100)]


def catalog(request: Request) -> MarketplaceCatalogQueryService:
    return cast(MarketplaceCatalogQueryService, request.app.state.catalog)


@router.get("/marketplace/v1/packages")
def packages(
    request: Request, q: str = "", limit: Limit = 20, cursor: str | None = None
) -> CursorPage[PackageSummary]:
    query = bounds(limit, q)
    binding = fingerprint("packages", query)
    rows = catalog(request).packages(query, limit, decode(cursor, "packages", binding))
    return CursorPage(
        items=[item for item, _ in rows[:limit]],
        next_cursor=encode(rows[limit - 1][1], "packages", binding) if len(rows) > limit else None,
    )


@router.get("/marketplace/v1/packages/{package_id}")
def package(request: Request, package_id: str) -> PackageDetail:
    return catalog(request).package(package_id)


@router.get("/marketplace/v1/packages/{package_id}/versions")
def versions(
    request: Request,
    package_id: str,
    limit: Limit = 20,
    cursor: str | None = None,
    include_unavailable: bool = False,
    include_prerelease: bool = True,
) -> CursorPage[PackageVersionSummary]:
    binding = fingerprint("versions", package_id, include_unavailable, include_prerelease)
    rows = catalog(request).versions(
        package_id,
        limit,
        decode(cursor, "versions", binding),
        include_unavailable,
        include_prerelease,
    )
    return CursorPage(
        items=[item for item, _ in rows[:limit]],
        next_cursor=encode(rows[limit - 1][1], "versions", binding) if len(rows) > limit else None,
    )


@router.get("/marketplace/v1/packages/{package_id}/versions/{version}")
def release(request: Request, package_id: str, version: str) -> PackageReleaseDetail:
    return catalog(request).release(package_id, version)


class ArtifactResponse(StreamingResponse):
    def __init__(self, stream: VerifiedArtifactStream) -> None:
        self.stream = stream
        digest = base64.b64encode(bytes.fromhex(stream.archive_sha256)).decode("ascii")
        super().__init__(
            stream.chunks(),
            media_type="application/octet-stream",
            headers={
                "Content-Length": str(stream.size_bytes),
                "Content-Disposition": 'attachment; filename="artifact.nervos"',
                "Content-Digest": f"sha-256=:{digest}:",
                "Cache-Control": "no-store, no-transform",
            },
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await run_in_threadpool(self.stream.close)


@router.get("/marketplace/v1/packages/{package_id}/versions/{version}/artifact")
def artifact(
    request: Request,
    package_id: str,
    version: str,
    acknowledgement: Annotated[str | None, Header(alias="X-NervOS-Acknowledge-Yanked")] = None,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
) -> StreamingResponse:
    service = cast(ArtifactReadService, request.app.state.artifact_reads)
    return ArtifactResponse(service.open(package_id, version, acknowledgement, range_header))


@router.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "live"}


@router.get("/health/ready")
def ready(request: Request) -> JSONResponse:
    dependency = cast(DependencyReadiness, request.app.state.readiness)
    ok = dependency.ready()
    return JSONResponse({"status": "ready" if ok else "not_ready"}, status_code=200 if ok else 503)
