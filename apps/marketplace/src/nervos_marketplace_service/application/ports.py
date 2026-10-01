"""Read ports; no ORM, HTTP or S3 objects cross these boundaries."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from nervos_marketplace_service.domain.catalog import (
    PackageDetail,
    PackageReleaseDetail,
    PackageSummary,
    PackageVersionSummary,
)


@dataclass(frozen=True)
class CatalogPosition:
    exact: int = 0
    rank: int = 0
    package_id: str = ""
    version: str = ""


class CatalogRepository(Protocol):
    def packages(
        self, query: str, limit: int, after: CatalogPosition | None
    ) -> list[tuple[PackageSummary, CatalogPosition]]: ...
    def package(self, package_id: str) -> PackageDetail: ...
    def versions(
        self,
        package_id: str,
        limit: int,
        after: CatalogPosition | None,
        include_unavailable: bool,
        include_prerelease: bool,
    ) -> list[tuple[PackageVersionSummary, CatalogPosition]]: ...
    def release(self, package_id: str, version: str) -> PackageReleaseDetail: ...


@dataclass(frozen=True)
class ArtifactStat:
    size_bytes: int


class VerifiedArtifactStream(Protocol):
    size_bytes: int
    archive_sha256: str

    def chunks(self) -> Iterator[bytes]: ...
    def close(self) -> None: ...


class ArtifactStore(Protocol):
    def stat(self, archive_sha256: str) -> ArtifactStat: ...
    def open_verified(self, archive_sha256: str, expected_size: int) -> VerifiedArtifactStream: ...
    def close(self) -> None: ...


class DependencyReadiness(Protocol):
    def ready(self) -> bool: ...
