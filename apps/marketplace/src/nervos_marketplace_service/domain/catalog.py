"""Public catalog values; storage IDs and storage locations are absent."""

from datetime import datetime
from enum import StrEnum

from nervos_core.domain.package_installation import validate_sha256
from nervos_core.domain.packages import PackageVersion, validate_package_id
from pydantic import BaseModel, ConfigDict, Field, field_validator

from nervos_marketplace_service.domain.errors import MarketplaceError

MAX_ARCHIVE_BYTES = 256 * 1024 * 1024


class PublicationState(StrEnum):
    READY = "ready"
    PUBLISHED = "published"


class DistributionState(StrEnum):
    AVAILABLE = "available"
    YANKED = "yanked"
    REVOKED = "revoked"


def identity(package_id: str, version: str | None = None) -> None:
    try:
        validate_package_id(package_id)
        if version is not None:
            PackageVersion(version)
    except ValueError:
        raise MarketplaceError("invalid_request") from None


class CatalogValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PackageSummary(CatalogValue):
    package_id: str
    display_name: str
    summary: str
    listing_revision: int
    latest_stable_version: str | None


class PackageDetail(PackageSummary):
    description: str
    listing_updated_at: datetime
    has_available_release: bool


class PackageVersionSummary(CatalogValue):
    exact_version: str
    published_at: datetime
    distribution_state: DistributionState
    status_revision: int


class Compatibility(CatalogValue):
    runtime_language: str
    runtime_python: str
    nervos_min_version: str
    nervos_max_version: str


class PackageReleaseDetail(PackageVersionSummary):
    package_id: str
    archive_sha256: str
    content_digest: str
    signer_fingerprint: str
    size_bytes: int = Field(gt=0, le=MAX_ARCHIVE_BYTES)
    manifest_version: int
    compatibility: Compatibility
    status_updated_at: datetime
    observed_at: datetime

    @field_validator("archive_sha256", "content_digest", "signer_fingerprint")
    @classmethod
    def digest(cls, value: str) -> str:
        return validate_sha256(value)


class CursorPage[T](CatalogValue):
    items: list[T]
    next_cursor: str | None = None
