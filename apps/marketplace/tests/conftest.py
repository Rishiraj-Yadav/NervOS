"""Integration fixtures are opt-in and fail when required services are unavailable."""

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from nervos_marketplace_service.config import MarketplaceSettings
from pydantic import SecretStr
from sqlalchemy import create_engine, insert, text
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def settings(tmp_path: Path) -> MarketplaceSettings:
    return MarketplaceSettings(
        environment="test",
        database_dsn=SecretStr(
            "postgresql+psycopg://fixture:fixture@127.0.0.1:5432/marketplace_test"
        ),
        s3_endpoint_url="http://127.0.0.1:8333",
        s3_region="us-east-1",
        s3_bucket="marketplace-test",
        s3_access_key_id=SecretStr("fixture-reader"),
        s3_secret_access_key=SecretStr("synthetic-reader-password"),
        artifact_temp_directory=tmp_path,
    )


@pytest.fixture(scope="session")
def admin_engine() -> Iterator[Engine]:
    dsn = os.environ.get("NERVOS_MARKETPLACE_TEST_DATABASE_DSN")
    if not dsn or "marketplace_test" not in dsn:
        pytest.fail(
            "Explicit disposable Marketplace test DSN is required; no developer DB fallback"
        )
    engine = create_engine(dsn, hide_parameters=True)
    migration = Config(str(ROOT / "apps/marketplace/alembic.ini"))
    previous = os.environ.get("NERVOS_MARKETPLACE_DATABASE_DSN")
    os.environ["NERVOS_MARKETPLACE_DATABASE_DSN"] = dsn
    try:
        command.upgrade(migration, "head")
        with engine.begin() as connection:
            connection.execute(
                text("CREATE ROLE mp_reader LOGIN PASSWORD 'synthetic-reader-password'")
            )
            connection.execute(text("GRANT USAGE ON SCHEMA public TO mp_reader"))
            connection.execute(text("GRANT SELECT ON ALL TABLES IN SCHEMA public TO mp_reader"))
        yield engine
    finally:
        engine.dispose()
        if previous is None:
            os.environ.pop("NERVOS_MARKETPLACE_DATABASE_DSN", None)
        else:
            os.environ["NERVOS_MARKETPLACE_DATABASE_DSN"] = previous


@pytest.fixture
def database(admin_engine: Engine) -> Engine:
    with admin_engine.begin() as connection:
        connection.execute(
            text("TRUNCATE package_releases,package_listings,artifacts,package_projects")
        )
    return admin_engine


def insert_fixture(
    engine: Engine,
    package_id: str,
    version: str = "1.2.3",
    state: str = "available",
    publication: str = "published",
) -> str:
    from nervos_core.domain.packages import PackageVersion
    from nervos_marketplace_service.domain.semver_order import precedence_key
    from nervos_marketplace_service.infrastructure.models import (
        ArtifactRow,
        PackageListingRow,
        PackageProjectRow,
        PackageReleaseRow,
    )

    from .fixtures import NOW, signed_package

    fixture = signed_package(package_id, version)
    verified = fixture.verified
    with engine.begin() as connection:
        project = connection.execute(
            text("SELECT id FROM package_projects WHERE package_id=:id"), {"id": package_id}
        ).scalar_one_or_none()
        if project is None:
            project = uuid4()
            connection.execute(
                insert(PackageProjectRow).values(id=project, package_id=package_id, created_at=NOW)
            )
            connection.execute(
                insert(PackageListingRow).values(
                    project_id=project,
                    display_name="Invoice Robot",
                    summary="Accounting Ledger",
                    description="Synthetic reconciliation automation",
                    revision=1,
                    updated_at=NOW,
                )
            )
        connection.execute(
            insert(ArtifactRow).values(
                archive_sha256=verified.archive_digest, size_bytes=len(fixture.raw), created_at=NOW
            )
        )
        parsed = PackageVersion(version)
        connection.execute(
            insert(PackageReleaseRow).values(
                id=uuid4(),
                project_id=project,
                exact_version=version,
                archive_sha256=verified.archive_digest,
                content_digest=verified.content_digest,
                signer_fingerprint=verified.signer_fingerprint,
                manifest_version=1,
                manifest_bytes=fixture.manifest,
                runtime_language="python",
                runtime_python=">=3.12,<4",
                nervos_min_version="0.1.0",
                nervos_max_version="0.1.0",
                semver_key=precedence_key(parsed),
                is_prerelease=bool(parsed.prerelease),
                publication_state=publication,
                distribution_state=state,
                status_revision=1,
                published_at=NOW if publication == "published" else None,
                status_updated_at=NOW,
            )
        )
    return verified.archive_digest
