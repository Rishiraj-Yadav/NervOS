"""Integration tests for Stage G4 package lifecycle mutations: create instance, config patch, rebind, rollback, and removal."""

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
)
from nervos_core.application.package_signing import Ed25519PackageSigner
from nervos_core.application.package_storage import PackageStore
from nervos_core.application.package_verification import verify_package
from nervos_core.domain.package_installation import (
    PackageInstallAuthorization,
    PackageInstallStatus,
)
from nervos_core.domain.package_query import (
    ImmutableConfigViolation,
    PackageHasBoundInstances,
    PackageRemovalOutcome,
    StaleConfigRevision,
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
        key = f"environments/{verified.content_digest}"
        dest = self._root / key
        dest.mkdir(parents=True, exist_ok=True)
        return EnvironmentIdentity("{}", verified.content_digest, key), dest


class FakeHealthChecker:
    def check(self, *, environment: Path, entrypoint: str, expected_sdk_api_version: str) -> None:
        pass


@pytest.fixture
def test_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("NERVOS_ENVIRONMENT", "test")
    monkeypatch.setenv("NERVOS_DATABASE_PATH", str(db_path))
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "head")

    engine = create_sqlite_engine(db_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, username, password_hash, role, is_active, created_at, updated_at) "
                "VALUES (1, 'alice', X'00', 'operator', 1, '2026-09-28 12:00:00', '2026-09-28 12:00:00')"
            )
        )

    store = PackageStore(tmp_path / "packages")
    registry = SqlAlchemyPackageRegistryPersistence(engine)

    import hashlib

    sdk_whl = tmp_path / "nervos_sdk-0.1.0-py3-none-any.whl"
    sdk_whl.write_bytes(b"sdk")
    sdk_sha = hashlib.sha256(b"sdk").hexdigest()

    host_whl = tmp_path / "nervos_package_host-0.1.0-py3-none-any.whl"
    host_whl.write_bytes(b"host")
    host_sha = hashlib.sha256(b"host").hexdigest()

    runtime = PackageRuntimeArtifacts(
        sdk=RuntimeWheelArtifact(sdk_whl, "nervos-sdk", "0.1.0", sdk_sha),
        host=RuntimeWheelArtifact(host_whl, "nervos-package-host", "0.1.0", host_sha),
    )
    env_builder = FakeEnvironmentBuilder(store.environments_root, runtime)
    health = FakeHealthChecker()
    service = PackageApplicationService(registry, store, env_builder, health, clock=_now)
    return service, registry, store, engine


def test_package_instance_creation_and_config_patch(test_setup) -> None:
    service, _, _, _ = test_setup

    schema_with_immutable = b"""{
      "type": "object",
      "properties": {
        "account_id": {"type": "string", "x-nervos-immutable": true},
        "batch_size": {"type": "integer", "default": 10, "x-nervos-immutable": false}
      },
      "required": ["account_id"],
      "additionalProperties": false
    }
    """
    pkg_bytes = package_build_bytes(
        PackageBuildInputs(
            manifest_bytes=VALID_MANIFEST,
            config_schema_bytes=schema_with_immutable,
            agent_wheel_bytes=valid_wheel(),
        ),
        signer=_signer(),
    )
    verified = verify_package(archive_bytes=pkg_bytes)
    auth = PackageInstallAuthorization(
        package_id=verified.manifest.package_id,
        package_version=verified.manifest.package_version,
        content_digest=verified.content_digest,
        signer_fingerprint=verified.signer_fingerprint,
        archive_digest=verified.archive_digest,
        approved_by_user_id=1,
        approved_at=_now(),
    )
    res = service.install(io.BytesIO(pkg_bytes), auth)
    assert res.package.status is PackageInstallStatus.ACTIVE

    # Create package instance
    instance, binding = service.create_package_instance(
        owner_user_id=1,
        package_id="com.acme.invoice",
        package_version="1.2.3",
        display_name="Invoice Agent",
        model_provider="anthropic",
        model_name="claude-3-5-sonnet",
        config={"account_id": "ACC100", "batch_size": 25},
    )
    assert instance.id > 0
    assert instance.definition_id.agent_key == "com.acme.invoice"
    assert binding.config_revision == 1
    assert '"account_id":"ACC100"' in binding.effective_config_json
    assert '"batch_size":25' in binding.effective_config_json

    # Patch config (mutable field)
    new_binding = service.update_instance_config(
        owner_user_id=1,
        agent_instance_id=instance.id,
        config={"account_id": "ACC100", "batch_size": 50},
        expected_config_revision=1,
    )
    assert new_binding.config_revision == 2
    assert '"batch_size":50' in new_binding.effective_config_json

    # Stale revision fails
    with pytest.raises(StaleConfigRevision):
        service.update_instance_config(
            owner_user_id=1,
            agent_instance_id=instance.id,
            config={"account_id": "ACC100", "batch_size": 75},
            expected_config_revision=1,
        )

    # Modifying immutable field fails
    with pytest.raises(ImmutableConfigViolation):
        service.update_instance_config(
            owner_user_id=1,
            agent_instance_id=instance.id,
            config={"account_id": "ACC999", "batch_size": 50},
            expected_config_revision=2,
        )


def test_package_side_by_side_and_rebind(test_setup) -> None:
    service, _, _, _ = test_setup

    v1_manifest = VALID_MANIFEST
    v2_manifest = VALID_MANIFEST.replace(b"package_version: 1.2.3", b"package_version: 2.0.0")

    v1_bytes = package_build_bytes(
        PackageBuildInputs(
            manifest_bytes=v1_manifest,
            config_schema_bytes=VALID_CONFIG_SCHEMA,
            agent_wheel_bytes=valid_wheel(),
        ),
        signer=_signer(),
    )
    v2_bytes = package_build_bytes(
        PackageBuildInputs(
            manifest_bytes=v2_manifest,
            config_schema_bytes=VALID_CONFIG_SCHEMA,
            agent_wheel_bytes=valid_wheel(),
        ),
        signer=_signer(),
    )

    ver1 = verify_package(archive_bytes=v1_bytes)
    ver2 = verify_package(archive_bytes=v2_bytes)

    service.install(
        io.BytesIO(v1_bytes),
        PackageInstallAuthorization(
            package_id=ver1.manifest.package_id,
            package_version=ver1.manifest.package_version,
            content_digest=ver1.content_digest,
            signer_fingerprint=ver1.signer_fingerprint,
            archive_digest=ver1.archive_digest,
            approved_by_user_id=1,
            approved_at=_now(),
        ),
    )
    service.install(
        io.BytesIO(v2_bytes),
        PackageInstallAuthorization(
            package_id=ver2.manifest.package_id,
            package_version=ver2.manifest.package_version,
            content_digest=ver2.content_digest,
            signer_fingerprint=ver2.signer_fingerprint,
            archive_digest=ver2.archive_digest,
            approved_by_user_id=1,
            approved_at=_now(),
        ),
    )

    instance, binding = service.create_package_instance(
        owner_user_id=1,
        package_id="com.acme.invoice",
        package_version="1.2.3",
        display_name="Invoice Agent",
        model_provider="anthropic",
        model_name="claude-3-5-sonnet",
        config={"greeting": "hi v1"},
    )
    assert instance.definition_id.agent_definition_version == "1.2.3"
    assert binding.config_revision == 1

    # Rebind to v2 with carry-forward
    rebound_instance, rebound_binding = service.rebind_instance(
        owner_user_id=1,
        agent_instance_id=instance.id,
        target_package_version="2.0.0",
        config=None,
        expected_config_revision=1,
    )
    assert rebound_instance.definition_id.agent_definition_version == "2.0.0"
    assert rebound_binding.config_revision == 2

    # Rollback to v1
    rolled_instance, rolled_binding = service.rebind_instance(
        owner_user_id=1,
        agent_instance_id=instance.id,
        target_package_version="1.2.3",
        config=None,
        expected_config_revision=2,
    )
    assert rolled_instance.definition_id.agent_definition_version == "1.2.3"
    assert rolled_binding.config_revision == 3


def test_package_removal_obligations(test_setup) -> None:
    service, _, _, _ = test_setup
    pkg_bytes = _built_bytes()
    ver = verify_package(archive_bytes=pkg_bytes)
    service.install(
        io.BytesIO(pkg_bytes),
        PackageInstallAuthorization(
            package_id=ver.manifest.package_id,
            package_version=ver.manifest.package_version,
            content_digest=ver.content_digest,
            signer_fingerprint=ver.signer_fingerprint,
            archive_digest=ver.archive_digest,
            approved_by_user_id=1,
            approved_at=_now(),
        ),
    )

    instance, _ = service.create_package_instance(
        owner_user_id=1,
        package_id="com.acme.invoice",
        package_version="1.2.3",
        display_name="Invoice Agent",
        model_provider="anthropic",
        model_name="claude-3-5-sonnet",
        config={},
    )

    # Blocked by bound instance
    with pytest.raises(PackageHasBoundInstances):
        service.remove_package("com.acme.invoice", "1.2.3")

    # Rebind away to a built-in or dummy (or if instance is unbound)
    # If we install v2 and rebind instance to v2:
    v2_bytes = package_build_bytes(
        PackageBuildInputs(
            manifest_bytes=VALID_MANIFEST.replace(
                b"package_version: 1.2.3", b"package_version: 2.0.0"
            ),
            config_schema_bytes=VALID_CONFIG_SCHEMA,
            agent_wheel_bytes=valid_wheel(),
        ),
        signer=_signer(),
    )
    ver2 = verify_package(archive_bytes=v2_bytes)
    service.install(
        io.BytesIO(v2_bytes),
        PackageInstallAuthorization(
            package_id=ver2.manifest.package_id,
            package_version=ver2.manifest.package_version,
            content_digest=ver2.content_digest,
            signer_fingerprint=ver2.signer_fingerprint,
            archive_digest=ver2.archive_digest,
            approved_by_user_id=1,
            approved_at=_now(),
        ),
    )
    service.rebind_instance(
        owner_user_id=1,
        agent_instance_id=instance.id,
        target_package_version="2.0.0",
        config=None,
        expected_config_revision=1,
    )

    # Now v1 has 0 bound instances and 0 runs -> immediate removal
    outcome = service.remove_package("com.acme.invoice", "1.2.3")
    assert outcome is PackageRemovalOutcome.REMOVED


def test_environment_retained_when_terminal_run_references_it(test_setup) -> None:
    service, _, store, engine = test_setup
    pkg_bytes = _built_bytes()
    ver = verify_package(archive_bytes=pkg_bytes)
    service.install(
        io.BytesIO(pkg_bytes),
        PackageInstallAuthorization(
            package_id=ver.manifest.package_id,
            package_version=ver.manifest.package_version,
            content_digest=ver.content_digest,
            signer_fingerprint=ver.signer_fingerprint,
            archive_digest=ver.archive_digest,
            approved_by_user_id=1,
            approved_at=_now(),
        ),
    )

    instance, _ = service.create_package_instance(
        owner_user_id=1,
        package_id="com.acme.invoice",
        package_version="1.2.3",
        display_name="Invoice Agent",
        model_provider="anthropic",
        model_name="claude-3-5-sonnet",
        config={},
    )

    # Submit run via AgentService and mark terminal
    from nervos_core.application.agent_definitions import create_composite_agent_definition_resolver
    from nervos_core.application.agents import AgentService
    from nervos_core.infrastructure.database import create_session_factory
    from nervos_core.infrastructure.database.agents import SqlAlchemyAgentPersistence
    from nervos_core.infrastructure.database.jobs import SqlAlchemyJobPersistence
    from nervos_core.infrastructure.database.packages import SqlInstalledPackageDefinitionSource
    from nervos_models import compose_model_providers

    known_providers = compose_model_providers(None, None).catalog
    resolver = create_composite_agent_definition_resolver(
        [SqlInstalledPackageDefinitionSource(engine)]
    )
    agent_service = AgentService(
        SqlAlchemyAgentPersistence(create_session_factory(engine)),
        resolver,
        _now,
        known_providers,
        SqlAlchemyJobPersistence(engine),
    )
    run = agent_service.submit_run(1, instance.id, "run input")
    finished_time = datetime(2026, 9, 28, 12, 1, 0, tzinfo=UTC)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE runs SET status = 'cancelled', finished_at = :n WHERE id = :id"),
            {"n": finished_time, "id": run.id},
        )

    # Rebind instance to v2
    v2_bytes = package_build_bytes(
        PackageBuildInputs(
            manifest_bytes=VALID_MANIFEST.replace(
                b"package_version: 1.2.3", b"package_version: 2.0.0"
            ),
            config_schema_bytes=VALID_CONFIG_SCHEMA,
            agent_wheel_bytes=valid_wheel(),
        ),
        signer=_signer(),
    )
    ver2 = verify_package(archive_bytes=v2_bytes)
    service.install(
        io.BytesIO(v2_bytes),
        PackageInstallAuthorization(
            package_id=ver2.manifest.package_id,
            package_version=ver2.manifest.package_version,
            content_digest=ver2.content_digest,
            signer_fingerprint=ver2.signer_fingerprint,
            archive_digest=ver2.archive_digest,
            approved_by_user_id=1,
            approved_at=_now(),
        ),
    )
    service.rebind_instance(
        owner_user_id=1,
        agent_instance_id=instance.id,
        target_package_version="2.0.0",
        config=None,
        expected_config_revision=1,
    )

    # Removal plan shows immediate removal possible but environment is retained for historical run
    outcome = service.remove_package("com.acme.invoice", "1.2.3")
    assert outcome is PackageRemovalOutcome.REMOVED

    # Environment directory and row are preserved for historical run
    env_dir = store.environments_root / f"environments/{ver.content_digest}"
    assert env_dir.exists()
