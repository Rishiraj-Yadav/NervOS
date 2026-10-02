"""Bounded catalog queries. Cursor encoding stays at the transport boundary."""

from nervos_core.domain.package_installation import validate_sha256

from nervos_marketplace_service.application.ports import CatalogPosition, CatalogRepository
from nervos_marketplace_service.domain.catalog import (
    PackageDetail,
    PackageReleaseDetail,
    PackageSummary,
    PackageVersionSummary,
    identity,
)
from nervos_marketplace_service.domain.errors import MarketplaceError


def bounds(limit: int, query: str = "") -> str:
    query = query.strip()
    if not 1 <= limit <= 100 or len(query.encode("utf-8")) > 256:
        raise MarketplaceError("invalid_request")
    return query


class MarketplaceCatalogQueryService:
    def __init__(self, repository: CatalogRepository) -> None:
        self.repository = repository

    def packages(
        self, query: str, limit: int, after: CatalogPosition | None
    ) -> list[tuple[PackageSummary, CatalogPosition]]:
        return self.repository.packages(bounds(limit, query), limit + 1, after)

    def package(self, package_id: str) -> PackageDetail:
        identity(package_id)
        return self.repository.package(package_id)

    def versions(
        self,
        package_id: str,
        limit: int,
        after: CatalogPosition | None,
        unavailable: bool,
        prerelease: bool,
    ) -> list[tuple[PackageVersionSummary, CatalogPosition]]:
        identity(package_id)
        bounds(limit)
        return self.repository.versions(package_id, limit + 1, after, unavailable, prerelease)

    def release(self, package_id: str, version: str) -> PackageReleaseDetail:
        identity(package_id, version)
        return self.repository.release(package_id, version)

    def storage_version(self, archive_sha256: str) -> str | None:
        validate_sha256(archive_sha256)
        return self.repository.storage_version(archive_sha256)
