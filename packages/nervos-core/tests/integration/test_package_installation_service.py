"""Integration tests for PackageApplicationService: full saga, authorization, and binding."""

# pyright: basic

from __future__ import annotations

import io
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
from nervos_core.application.package_environment import (
    EnvironmentIdentity,
    PackageEnvironmentBuilder,
    PackageRuntimeArtifacts,
    RuntimeWheelArtifact,
)
from nervos_core.application.package_installation import (
    PackageApplicationService,
    PackageAuthorizationMismatch,
)
from nervos_core.application.package_signing import Ed25519PackageSigner
from nervos_core.application.package_storage import PackageStore
from nervos_core.application.package_verification import verify_package
from nervos_core.domain.package_installation import (
    PackageInstallAuthorization,
    PackageInstallStatus,
)
from nervos_core.infrastructure.database import create_sqlite_engine
from nervos_core.infrastructure.database.packages import SqlAlchemyPackageRegistryPersistence
from package_fixtures import (  # pyright: ignore[reportMissingImports]
    TEST_SIGNING_SEED,
    VALID_CONFIG_SCHEMA,
    VALID_MANIFEST,
    valid_wheel,
)
from sqlalchemy import text


def _now() -> datetime:
    return datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)


def _signer() -> Ed25519PackageSigner:
    return Ed25519PackageSigner.from_private_bytes(TEST_SIGNING_SEED)


def _built_bytes() -> bytes:
    return package_build_bytes(
        PackageBuildInputs(
            manifest_bytes=VALID_MANIFEST,
            config_schema_bytes=VALID_CONFIG_SCHEMA,
            agent_wheel_bytes=valid_wheel(),
        ),
        signer=_signer(),
    )


class FakeEnvironmentBuilder(PackageEnvironmentBuilder):
    def __init__(self, store_root: Path, runtime: PackageRuntimeArtifacts) -> None:
        super().__init__(store_root, runtime)

    def build(self, verified, payload_root):
        key = "environments/" + "e" * 64
        dest = self._root / key
        dest.mkdir(parents=True, exist_ok=True)
        return EnvironmentIdentity("{}", "e" * 64, key), dest


class FakeHealthChecker:
    def check(self, *, environment: Path, entrypoint: str, expected_sdk_api_version: str) -> None:
        pass


ROOT = Path(__file__).resolve().parents[4]
ALEMBIC_INI = ROOT / "apps" / "api" / "alembic.ini"


@pytest.fixture
def migrated_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "install_service.db"
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


def test_package_application_service_full_install_and_binding(
    migrated_engine, tmp_path: Path
) -> None:
    store = PackageStore(tmp_path / "store")
    raw = _built_bytes()
    verified_pkg = verify_package(archive_bytes=raw)

    sdk_dummy = tmp_path / "nervos_sdk-0.1.0-py3-none-any.whl"
    sdk_dummy.write_bytes(b"sdk-wheel")
    host_dummy = tmp_path / "nervos_package_host-0.1.0-py3-none-any.whl"
    host_dummy.write_bytes(b"host-wheel")

    import hashlib

    runtime = PackageRuntimeArtifacts(
        sdk=RuntimeWheelArtifact(
            sdk_dummy, "nervos-sdk", "0.1.0", hashlib.sha256(b"sdk-wheel").hexdigest()
        ),
        host=RuntimeWheelArtifact(
            host_dummy, "nervos-package-host", "0.1.0", hashlib.sha256(b"host-wheel").hexdigest()
        ),
    )
    env_builder = FakeEnvironmentBuilder(tmp_path / "store", runtime)
    health_checker = FakeHealthChecker()
    persistence = SqlAlchemyPackageRegistryPersistence(migrated_engine)

    service = PackageApplicationService(
        registry=persistence,
        store=store,
        environment_builder=env_builder,
        health_checker=health_checker,
        clock=_now,
    )

    auth = PackageInstallAuthorization(
        package_id=verified_pkg.manifest.package_id,
        package_version=verified_pkg.manifest.package_version,
        content_digest=verified_pkg.content_digest,
        signer_fingerprint=verified_pkg.signer_fingerprint,
        approved_by_user_id=1,
        approved_at=_now(),
    )

    result = service.install(io.BytesIO(raw), auth)
    assert result.package.status is PackageInstallStatus.ACTIVE
    assert result.package.package_id == "com.acme.invoice"

    # Idempotent re-install
    idempotent = service.install(io.BytesIO(raw), auth)
    assert idempotent.package.id == result.package.id

    # Binding instance with config default application
    binding = service.bind_instance(
        owner_user_id=1,
        agent_instance_id=1,
        installed_package_version_id=result.package.id,
        config={},  # default "greeting": "hello" applied
    )
    assert binding.agent_instance_id == 1
    assert '"greeting":"hello"' in binding.effective_config_json

    revised_binding = service.bind_instance(
        owner_user_id=1,
        agent_instance_id=1,
        installed_package_version_id=result.package.id,
        config={"greeting": "revised"},
    )
    assert revised_binding.config_revision == 2
    assert '"greeting":"revised"' in revised_binding.effective_config_json


def test_authorization_mismatch_fails_before_install(migrated_engine, tmp_path: Path) -> None:
    store = PackageStore(tmp_path / "store")
    raw = _built_bytes()
    sdk_dummy = tmp_path / "nervos_sdk-0.1.0-py3-none-any.whl"
    sdk_dummy.write_bytes(b"sdk-wheel")
    host_dummy = tmp_path / "nervos_package_host-0.1.0-py3-none-any.whl"
    host_dummy.write_bytes(b"host-wheel")

    import hashlib

    runtime = PackageRuntimeArtifacts(
        sdk=RuntimeWheelArtifact(
            sdk_dummy, "nervos-sdk", "0.1.0", hashlib.sha256(b"sdk-wheel").hexdigest()
        ),
        host=RuntimeWheelArtifact(
            host_dummy, "nervos-package-host", "0.1.0", hashlib.sha256(b"host-wheel").hexdigest()
        ),
    )
    service = PackageApplicationService(
        registry=SqlAlchemyPackageRegistryPersistence(migrated_engine),
        store=store,
        environment_builder=FakeEnvironmentBuilder(tmp_path / "store", runtime),
        health_checker=FakeHealthChecker(),
        clock=_now,
    )
    mismatched_auth = PackageInstallAuthorization(
        package_id="com.acme.invoice",
        package_version="1.2.3",
        content_digest="0" * 64,  # wrong digest
        signer_fingerprint="f" * 64,
        approved_by_user_id=1,
        approved_at=_now(),
    )
    with pytest.raises(PackageAuthorizationMismatch):
        service.install(io.BytesIO(raw), mismatched_auth)
