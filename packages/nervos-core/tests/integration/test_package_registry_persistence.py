"""Integration tests for G3 SQL package registry persistence, definition source, and bindings."""

# pyright: basic

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
ALEMBIC_INI = ROOT / "apps" / "api" / "alembic.ini"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))

import pytest
from alembic import command
from alembic.config import Config
from nervos_core.application.package_builder import PackageBuildInputs, package_build_bytes
from nervos_core.application.package_signing import Ed25519PackageSigner
from nervos_core.application.package_verification import verify_package
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.execution import RunExecutionKind
from nervos_core.domain.package_installation import (
    PackageEnvironmentStatus,
    PackageInstallAuthorization,
    PackageInstallStatus,
)
from nervos_core.infrastructure.database import create_sqlite_engine
from nervos_core.infrastructure.database.packages import (
    PackageIdentityConflict,
    PackageInstallInProgress,
    PackageInstallRecordInput,
    PackageRegistryInvariantError,
    SqlAlchemyPackageRegistryPersistence,
    SqlInstalledPackageDefinitionSource,
    resolve_run_execution_snapshot_on_connection,
)
from package_fixtures import (  # pyright: ignore[reportMissingImports]
    OTHER_TEST_SIGNING_SEED,
    TEST_SIGNING_SEED,
    VALID_CONFIG_SCHEMA,
    VALID_MANIFEST,
    valid_wheel,
)
from sqlalchemy import text


def _now() -> datetime:
    return datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)


def _signer(seed: bytes = TEST_SIGNING_SEED) -> Ed25519PackageSigner:
    return Ed25519PackageSigner.from_private_bytes(seed)


def _verified(manifest: bytes = VALID_MANIFEST, seed: bytes = TEST_SIGNING_SEED):
    raw = package_build_bytes(
        PackageBuildInputs(
            manifest_bytes=manifest,
            config_schema_bytes=VALID_CONFIG_SCHEMA,
            agent_wheel_bytes=valid_wheel(),
        ),
        signer=_signer(seed),
    )
    return verify_package(archive_bytes=raw)


ROOT = Path(__file__).resolve().parents[4]
ALEMBIC_INI = ROOT / "apps" / "api" / "alembic.ini"


@pytest.fixture
def migrated_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "registry.db"
    monkeypatch.setenv("NERVOS_ENVIRONMENT", "test")
    monkeypatch.setenv("NERVOS_DATABASE_PATH", str(db_path))
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, "head")
    engine = create_sqlite_engine(db_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users(id, username, password_hash, role, is_active, created_at, updated_at) "
                "VALUES(1, 'owner', X'00', 'admin', 1, '2026-09-28 12:00:00', '2026-09-28 12:00:00')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO agent_instances(id, owner_user_id, agent_key, agent_definition_version, display_name, enabled, model_provider, model_name, created_at, updated_at) "
                "VALUES(1, 1, 'com.acme.invoice', '1.2.3', 'Invoice Agent', 1, 'anthropic', 'claude-3-5', '2026-09-28 12:00:00', '2026-09-28 12:00:00')"
            )
        )
    return engine


def test_registry_install_and_activation_lifecycle(migrated_engine) -> None:
    persistence = SqlAlchemyPackageRegistryPersistence(migrated_engine)
    verified_pkg = _verified()
    auth = PackageInstallAuthorization(
        package_id=verified_pkg.manifest.package_id,
        package_version=verified_pkg.manifest.package_version,
        content_digest=verified_pkg.content_digest,
        signer_fingerprint=verified_pkg.signer_fingerprint,
        approved_by_user_id=1,
        approved_at=_now(),
    )
    command_input = PackageInstallRecordInput(
        verified=verified_pkg,
        manifest_bytes=VALID_MANIFEST,
        config_schema_bytes=VALID_CONFIG_SCHEMA,
        dependency_lock_bytes=verified_pkg.dependency_lock.canonical_bytes(),
        staging_key="staging/op-1",
        authorization=auth,
        now=_now(),
    )

    installed = persistence.begin_install(command_input)
    assert installed.status is PackageInstallStatus.INSTALLING

    # In-progress rejection
    with pytest.raises(PackageInstallInProgress):
        persistence.begin_install(command_input)

    persistence.mark_installed(installed.id, "packages/com_acme_invoice/1_2_3/abc", _now())
    env = persistence.create_or_get_environment(
        environment_digest="e" * 64,
        environment_key_json="{}",
        environment_key="environments/" + "e" * 64,
        sdk_version="0.1.0",
        sdk_wheel_digest="1" * 64,
        host_version="0.1.0",
        host_wheel_digest="2" * 64,
        now=_now(),
    )
    persistence.mark_environment_ready(env.id, _now())
    persistence.attach_environment(installed.id, env.id, _now())
    active = persistence.mark_active(installed.id, _now())
    assert active.status is PackageInstallStatus.ACTIVE

    # SQL definition source resolves active package
    source = SqlInstalledPackageDefinitionSource(migrated_engine)
    definitions = source.list_definitions()
    assert len(definitions) == 1
    assert definitions[0].identity.agent_key == "com.acme.invoice"
    assert definitions[0].identity.agent_definition_version == "1.2.3"

    # Instance binding and Run execution snapshot resolution
    binding = persistence.bind_instance(
        owner_user_id=1,
        agent_instance_id=1,
        installed_id=installed.id,
        effective_config_json='{"greeting":"hello"}',
        effective_config_digest="d" * 64,
        config_schema_digest="a" * 64,
        now=_now(),
    )
    assert binding.agent_instance_id == 1

    with migrated_engine.connect() as conn:
        snapshot = resolve_run_execution_snapshot_on_connection(
            conn,
            agent_instance_id=1,
            definition_id=AgentDefinitionId("com.acme.invoice", "1.2.3"),
        )
        assert snapshot.execution_kind == RunExecutionKind.PACKAGE
        assert snapshot.installed_package_version_id == installed.id
        assert snapshot.package_content_digest == verified_pkg.content_digest
        assert snapshot.package_environment_digest == "e" * 64


def test_registry_conflict_on_different_content(migrated_engine) -> None:
    persistence = SqlAlchemyPackageRegistryPersistence(migrated_engine)
    first_verified = _verified()
    auth = PackageInstallAuthorization(
        package_id=first_verified.manifest.package_id,
        package_version=first_verified.manifest.package_version,
        content_digest=first_verified.content_digest,
        signer_fingerprint=first_verified.signer_fingerprint,
        approved_by_user_id=1,
        approved_at=_now(),
    )
    persistence.begin_install(
        PackageInstallRecordInput(
            verified=first_verified,
            manifest_bytes=VALID_MANIFEST,
            config_schema_bytes=VALID_CONFIG_SCHEMA,
            dependency_lock_bytes=first_verified.dependency_lock.canonical_bytes(),
            staging_key="staging/op-1",
            authorization=auth,
            now=_now(),
        )
    )

    # Different content under same package_id and package_version must fail closed
    second_verified = _verified(manifest=VALID_MANIFEST.replace(b"Acme Tools", b"Other Publisher"))
    second_auth = PackageInstallAuthorization(
        package_id=second_verified.manifest.package_id,
        package_version=second_verified.manifest.package_version,
        content_digest=second_verified.content_digest,
        signer_fingerprint=second_verified.signer_fingerprint,
        approved_by_user_id=1,
        approved_at=_now(),
    )
    with pytest.raises(PackageIdentityConflict):
        persistence.begin_install(
            PackageInstallRecordInput(
                verified=second_verified,
                manifest_bytes=VALID_MANIFEST,
                config_schema_bytes=VALID_CONFIG_SCHEMA,
                dependency_lock_bytes=second_verified.dependency_lock.canonical_bytes(),
                staging_key="staging/op-2",
                authorization=second_auth,
                now=_now(),
            )
        )


def test_failed_exact_install_retry(migrated_engine) -> None:
    persistence = SqlAlchemyPackageRegistryPersistence(migrated_engine)
    verified_pkg = _verified()
    auth = PackageInstallAuthorization(
        package_id=verified_pkg.manifest.package_id,
        package_version=verified_pkg.manifest.package_version,
        content_digest=verified_pkg.content_digest,
        signer_fingerprint=verified_pkg.signer_fingerprint,
        approved_by_user_id=1,
        approved_at=_now(),
    )
    command_input = PackageInstallRecordInput(
        verified=verified_pkg,
        manifest_bytes=VALID_MANIFEST,
        config_schema_bytes=VALID_CONFIG_SCHEMA,
        dependency_lock_bytes=verified_pkg.dependency_lock.canonical_bytes(),
        staging_key="staging/op-1",
        authorization=auth,
        now=_now(),
    )

    installed = persistence.begin_install(command_input)
    persistence.mark_failed(installed.id, "install_failed", "transient failure", _now())

    # Explicit retry on exact same identity and content is allowed on the same row
    retried = persistence.begin_install(
        PackageInstallRecordInput(
            verified=verified_pkg,
            manifest_bytes=VALID_MANIFEST,
            config_schema_bytes=VALID_CONFIG_SCHEMA,
            dependency_lock_bytes=verified_pkg.dependency_lock.canonical_bytes(),
            staging_key="staging/op-1-retry",
            authorization=auth,
            now=_now(),
        )
    )
    assert retried.id == installed.id
    assert retried.status is PackageInstallStatus.INSTALLING
