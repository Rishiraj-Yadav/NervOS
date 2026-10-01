"""Exact status admission; transport verification cannot authorize local execution."""

from nervos_marketplace_service.application.catalog_queries import MarketplaceCatalogQueryService
from nervos_marketplace_service.application.ports import ArtifactStore, VerifiedArtifactStream
from nervos_marketplace_service.domain.catalog import DistributionState, PackageReleaseDetail
from nervos_marketplace_service.domain.errors import MarketplaceError


def distribution_allowed(release: PackageReleaseDetail, acknowledgement: str | None) -> None:
    if release.distribution_state == DistributionState.REVOKED:
        raise MarketplaceError("release_revoked", 410)
    if (
        release.distribution_state == DistributionState.YANKED
        and acknowledgement != release.archive_sha256
    ):
        raise MarketplaceError("release_yanked", 409)


class ArtifactReadService:
    def __init__(self, catalog: MarketplaceCatalogQueryService, store: ArtifactStore) -> None:
        self.catalog = catalog
        self.store = store

    def open(
        self,
        package_id: str,
        version: str,
        acknowledgement: str | None,
        range_header: str | None = None,
    ) -> VerifiedArtifactStream:
        if range_header is not None:
            raise MarketplaceError("range_not_supported", 416)
        release = self.catalog.release(package_id, version)
        distribution_allowed(release, acknowledgement)
        stream = self.store.open_verified(release.archive_sha256, release.size_bytes)
        try:
            current = self.catalog.release(package_id, version)
            distribution_allowed(current, acknowledgement)
            if (current.archive_sha256, current.size_bytes) != (
                release.archive_sha256,
                release.size_bytes,
            ):
                raise MarketplaceError("artifact_unavailable", 503)
            return stream
        except BaseException:
            stream.close()
            raise
