"""Independent hosted metadata. Local NervOS Base is deliberately not imported."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class HostedBase(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class PackageProjectRow(HostedBase):
    __tablename__ = "package_projects"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    package_id: Mapped[str] = mapped_column(String(128, collation="C"), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    publisher_id: Mapped[UUID] = mapped_column(Uuid)
    ownership_revision: Mapped[int] = mapped_column(Integer, server_default="1")
    visibility: Mapped[str] = mapped_column(String(16), server_default="visible")
    __table_args__ = (
        CheckConstraint(
            "length(package_id) BETWEEN 1 AND 128 AND package_id NOT LIKE 'nervos.%'",
            name="bounded_identity",
        ),
    )


class PackageListingRow(HostedBase):
    __tablename__ = "package_listings"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("package_projects.id", ondelete="RESTRICT"), primary_key=True
    )
    display_name: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('simple', display_name), 'A') || "
            "setweight(to_tsvector('simple', summary), 'B') || "
            "setweight(to_tsvector('simple', description), 'C')",
            persisted=True,
        ),
    )
    __table_args__ = (
        CheckConstraint(
            "length(display_name) BETWEEN 1 AND 256 AND octet_length(display_name) <= 1024",
            name="display_bound",
        ),
        CheckConstraint("octet_length(summary) <= 4096", name="summary_bound"),
        CheckConstraint("octet_length(description) <= 32768", name="description_bound"),
        CheckConstraint("revision > 0", name="revision_positive"),
        Index("ix_listing_search", "search_vector", postgresql_using="gin"),
    )


class ArtifactRow(HostedBase):
    __tablename__ = "artifacts"
    archive_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    storage_version_id: Mapped[str | None] = mapped_column(String(1024))
    __table_args__ = (
        CheckConstraint("archive_sha256 ~ '^[0-9a-f]{64}$'", name="digest"),
        CheckConstraint("size_bytes BETWEEN 1 AND 268435456", name="size_bound"),
        CheckConstraint(
            "storage_version_id IS NULL OR (storage_version_id <> 'null' AND "
            "length(storage_version_id) BETWEEN 1 AND 1024 AND "
            "storage_version_id ~ '^[A-Za-z0-9._~+/=-]+$')",
            name="storage_version",
        ),
    )


class PackageReleaseRow(HostedBase):
    __tablename__ = "package_releases"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("package_projects.id", ondelete="RESTRICT"))
    original_publisher_id: Mapped[UUID] = mapped_column(Uuid)
    exact_version: Mapped[str] = mapped_column(String(64, collation="C"))
    archive_sha256: Mapped[str] = mapped_column(
        ForeignKey("artifacts.archive_sha256", ondelete="RESTRICT"), unique=True
    )
    content_digest: Mapped[str] = mapped_column(String(64))
    signer_fingerprint: Mapped[str] = mapped_column(String(64))
    manifest_version: Mapped[int] = mapped_column(Integer)
    manifest_bytes: Mapped[bytes] = mapped_column(LargeBinary)
    runtime_language: Mapped[str] = mapped_column(String(16))
    runtime_python: Mapped[str] = mapped_column(String(64))
    nervos_min_version: Mapped[str] = mapped_column(String(64))
    nervos_max_version: Mapped[str] = mapped_column(String(64))
    semver_key: Mapped[bytes] = mapped_column(LargeBinary)
    is_prerelease: Mapped[bool] = mapped_column(Boolean)
    publication_state: Mapped[str] = mapped_column(String(16))
    distribution_state: Mapped[str] = mapped_column(String(16))
    status_revision: Mapped[int] = mapped_column(Integer)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("project_id", "exact_version"),
        CheckConstraint("length(exact_version) BETWEEN 1 AND 64", name="version_bound"),
        CheckConstraint(
            "content_digest ~ '^[0-9a-f]{64}$' AND signer_fingerprint ~ '^[0-9a-f]{64}$'",
            name="evidence_digests",
        ),
        CheckConstraint(
            "manifest_version = 1 AND octet_length(manifest_bytes) BETWEEN 1 AND 1048576",
            name="manifest_bound",
        ),
        CheckConstraint("octet_length(semver_key) BETWEEN 193 AND 2304", name="order_bound"),
        CheckConstraint("publication_state IN ('ready','published')", name="publication"),
        CheckConstraint(
            "distribution_state IN ('available','yanked','revoked')", name="distribution"
        ),
        CheckConstraint("status_revision > 0", name="revision_positive"),
        CheckConstraint(
            "(publication_state = 'published') = (published_at IS NOT NULL)",
            name="publication_timestamp",
        ),
        Index(
            "ix_release_published_order",
            "project_id",
            "semver_key",
            "exact_version",
            postgresql_where=publication_state == "published",
        ),
        Index(
            "ix_release_stable_order",
            "project_id",
            "semver_key",
            "exact_version",
            postgresql_where=(publication_state == "published")
            & (distribution_state == "available")
            & (is_prerelease.is_(False)),
        ),
    )
