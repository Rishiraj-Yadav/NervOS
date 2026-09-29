"""Concurrency tests for racing Run submission and AgentInstance rebind."""

# pyright: basic

from __future__ import annotations

import io
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
ALEMBIC_INI = ROOT / "apps" / "api" / "alembic.ini"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))

import pytest
from alembic import command
from alembic.config import Config
from nervos_core.application.agents import AgentService
from nervos_core.application.package_builder import PackageBuildInputs, package_build_bytes
from nervos_core.application.package_environment import (
    EnvironmentIdentity,
    PackageEnvironmentBuilder,
    PackageRuntimeArtifacts,
    RuntimeWheelArtifact,
)
from nervos_core.application.package_installation import PackageApplicationService
from nervos_core.application.package_signing import Ed25519PackageSigner
from nervos_core.application.package_storage import PackageStore
from nervos_core.application.package_verification import verify_package
from nervos_core.domain.package_installation import PackageInstallAuthorization
from nervos_core.infrastructure.database import create_session_factory, create_sqlite_engine
from nervos_core.infrastructure.database.agents import SqlAlchemyAgentPersistence
from nervos_core.infrastructure.database.jobs import SqlAlchemyJobPersistence
from nervos_core.infrastructure.database.packages import (
    SqlAlchemyPackageRegistryPersistence,
    SqlInstalledPackageDefinitionSource,
)
from nervos_models import compose_model_providers
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
def concurrent_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "concurrent.db"
    monkeypatch.setenv("NERVOS_ENVIRONMENT", "test")
    monkeypatch.setenv("NERVOS_DATABASE_PATH", str(db_path))
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "head")

    engine = create_sqlite_engine(db_path)
    session_factory = create_session_factory(engine)
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
    pkg_service = PackageApplicationService(registry, store, env_builder, health, clock=_now)

    known_providers = compose_model_providers(None, None).catalog
    from nervos_core.application.agent_definitions import create_composite_agent_definition_resolver

    definitions = create_composite_agent_definition_resolver(
        [SqlInstalledPackageDefinitionSource(engine)]
    )
    job_persistence = SqlAlchemyJobPersistence(engine)
    agent_service = AgentService(
        SqlAlchemyAgentPersistence(session_factory),
        definitions,
        _now,
        known_providers,
        job_persistence,
    )

    return pkg_service, agent_service, engine


def test_racing_submission_and_rebind_produces_consistent_snapshots(concurrent_setup) -> None:
    pkg_service, agent_service, engine = concurrent_setup

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

    pkg_service.install(
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
    pkg_service.install(
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

    instance, _ = pkg_service.create_package_instance(
        owner_user_id=1,
        package_id="com.acme.invoice",
        package_version="1.2.3",
        display_name="Invoice Agent",
        model_provider="anthropic",
        model_name="claude-3-5-sonnet",
        config={"greeting": "v1-config"},
    )

    def submit_worker():
        return agent_service.submit_run(1, instance.id, "run input")

    def rebind_worker():
        return pkg_service.rebind_instance(
            owner_user_id=1,
            agent_instance_id=instance.id,
            target_package_version="2.0.0",
            config={"greeting": "v2-config"},
            expected_config_revision=1,
        )

    from nervos_core.application.agents import AgentInstanceUnavailable

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(submit_worker)
        f2 = executor.submit(rebind_worker)
        f2.result()
        try:
            run = f1.result()
        except AgentInstanceUnavailable:
            # Rebind committed between prepare_submission and admission: safely rejected to prevent tearing
            run = None

    if run is not None:
        # Verify Run executable is 100% consistent with either v1 or v2
        with engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT agent_definition_version, effective_config_json FROM runs WHERE id = :id"
                    ),
                    {"id": run.id},
                )
                .mappings()
                .one()
            )

            version = row["agent_definition_version"]
            config_json = row["effective_config_json"]

            if version == "1.2.3":
                assert "v1-config" in config_json
            elif version == "2.0.0":
                assert "v2-config" in config_json
            else:
                pytest.fail(f"Unexpected run version: {version}")
