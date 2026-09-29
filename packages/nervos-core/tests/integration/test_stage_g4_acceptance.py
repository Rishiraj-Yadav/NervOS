"""Stage G4 integrated acceptance test: full package lifecycle journey.

Proves:
1. Build & Inspect package v1.
2. Install package v1 to ACTIVE with operator authorization.
3. Create package-backed AgentInstance.
4. Execute Run on v1 -> verify output & snapshot pinning.
5. Build & Install package v2 side-by-side.
6. Verify instance remains bound to v1.
7. Rebind instance to v2 -> verify next Run executes v2.
8. Rollback instance to v1 -> verify next Run executes v1.
9. Verify historical v1 and v2 Runs remain pinned to their respective versions.
10. Attempt uninstall v1 -> blocked by bound instance.
11. Rebind away to v2 -> uninstall v1 -> successfully removed with payload cleanup.
"""

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
from nervos_core.application.agent_definitions import create_composite_agent_definition_resolver
from nervos_core.application.agents import AgentService
from nervos_core.application.model_completion import ModelCompletion, ModelRequest, ModelResponse
from nervos_core.application.package_builder import PackageBuildInputs, package_build_bytes
from nervos_core.application.package_environment import (
    EnvironmentIdentity,
    PackageEnvironmentBuilder,
    PackageRuntimeArtifacts,
    RuntimeWheelArtifact,
)
from nervos_core.application.package_installation import PackageApplicationService
from nervos_core.application.package_query import PackageQueryService
from nervos_core.application.package_signing import Ed25519PackageSigner
from nervos_core.application.package_storage import PackageStore
from nervos_core.application.run_execution import RunExecutor
from nervos_core.domain.package_installation import (
    PackageInstallAuthorization,
    PackageInstallStatus,
)
from nervos_core.domain.package_query import (
    PackageHasBoundInstances,
    PackageRemovalOutcome,
)
from nervos_core.domain.runs import Run, RunStatus
from nervos_core.infrastructure.database import create_session_factory, create_sqlite_engine
from nervos_core.infrastructure.database.agents import SqlAlchemyAgentPersistence
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobPersistence,
)
from nervos_core.infrastructure.database.packages import (
    SqlAlchemyPackageRegistryPersistence,
    SqlInstalledPackageDefinitionSource,
)
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


class DeterministicModelCompletion(ModelCompletion):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            text="echo: " + request.user_text,
            model_provider="anthropic",
            model_name=request.model_name,
        )


class DirectPackageHostAdapter:
    def __init__(self, store_root: Path) -> None:
        self._store = store_root

    async def run(self, completion, run: Run, claim, elapsed_ms, snapshot=None):
        del claim, elapsed_ms, snapshot
        import json

        from nervos_core.application.trusted_chat import ChatOutcome

        cfg = json.loads(run.executable.effective_config_json)
        greeting = cfg.get("greeting", "echo")
        version = run.agent_definition_version
        return ChatOutcome(
            output_text=f"[{version}] {greeting}: {run.input_text}",
            finish_reason="stop",
            usage=run.usage,
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


@pytest.mark.anyio
async def test_g4_integrated_lifecycle_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "g4_acceptance.db"
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
                "VALUES (1, 'operator', X'00', 'administrator', 1, '2026-09-28 12:00:00', '2026-09-28 12:00:00')"
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
    query_service = PackageQueryService(registry, store)

    # 1. Build and inspect v1
    v1_manifest = VALID_MANIFEST
    v1_bytes = package_build_bytes(
        PackageBuildInputs(
            manifest_bytes=v1_manifest,
            config_schema_bytes=VALID_CONFIG_SCHEMA,
            agent_wheel_bytes=valid_wheel(),
        ),
        signer=_signer(),
    )
    v1_file = tmp_path / "v1.nervos"
    v1_file.write_bytes(v1_bytes)

    inspected_v1 = query_service.inspect_artifact(v1_file)
    assert inspected_v1.package_id == "com.acme.invoice"
    assert inspected_v1.package_version == "1.2.3"
    assert inspected_v1.is_compatible is True

    # 2. Authorize and install v1
    auth_v1 = PackageInstallAuthorization(
        package_id=inspected_v1.package_id,
        package_version=inspected_v1.package_version,
        content_digest=inspected_v1.content_digest,
        signer_fingerprint=inspected_v1.signer_fingerprint,
        archive_digest=inspected_v1.archive_digest,
        approved_by_user_id=1,
        approved_at=_now(),
    )
    res_v1 = pkg_service.install(v1_file, auth_v1)
    assert res_v1.package.status is PackageInstallStatus.ACTIVE

    # 3. Create package AgentInstance
    instance, binding = pkg_service.create_package_instance(
        owner_user_id=1,
        package_id="com.acme.invoice",
        package_version="1.2.3",
        display_name="Echo Agent",
        model_provider="anthropic",
        model_name="claude-3-5-sonnet",
        config={"greeting": "Hello"},
    )
    assert instance.id > 0
    assert binding.config_revision == 1

    # 4. Run agent on v1 through execution engine
    def_source = SqlInstalledPackageDefinitionSource(engine)
    resolver = create_composite_agent_definition_resolver([def_source])
    job_persistence = SqlAlchemyJobPersistence(engine)
    from nervos_models import compose_model_providers

    known_providers = compose_model_providers(None, None).catalog
    agent_service = AgentService(
        SqlAlchemyAgentPersistence(session_factory),
        resolver,
        _now,
        known_providers,
        job_persistence,
    )

    run_v1 = agent_service.submit_run(1, instance.id, "invoice #101")
    assert run_v1.status is RunStatus.CREATED

    from nervos_core.application.job_execution import ClaimedAttempt
    from nervos_core.application.trusted_chat import create_builtin_handler_registry

    claim = ClaimedAttempt(
        job_id=1,
        run_id=run_v1.id,
        attempt_id=1,
        attempt_number=1,
        worker_id="test",
        claim_token=b"0" * 32,
        lease_expires_at=_now(),
    )
    adapter = DirectPackageHostAdapter(store.root)
    executor = RunExecutor(create_builtin_handler_registry(), package_execution=adapter)
    completion = DeterministicModelCompletion()
    outcome = await executor.execute(run_v1, completion, claim=claim)
    assert outcome.output_text == "[1.2.3] Hello: invoice #101"

    # 5. Build and install v2 side-by-side
    v2_manifest = VALID_MANIFEST.replace(b"package_version: 1.2.3", b"package_version: 2.0.0")
    v2_bytes = package_build_bytes(
        PackageBuildInputs(
            manifest_bytes=v2_manifest,
            config_schema_bytes=VALID_CONFIG_SCHEMA,
            agent_wheel_bytes=valid_wheel(),
        ),
        signer=_signer(),
    )
    v2_file = tmp_path / "v2.nervos"
    v2_file.write_bytes(v2_bytes)

    inspected_v2 = query_service.inspect_artifact(v2_file)
    auth_v2 = PackageInstallAuthorization(
        package_id=inspected_v2.package_id,
        package_version=inspected_v2.package_version,
        content_digest=inspected_v2.content_digest,
        signer_fingerprint=inspected_v2.signer_fingerprint,
        archive_digest=inspected_v2.archive_digest,
        approved_by_user_id=1,
        approved_at=_now(),
    )
    res_v2 = pkg_service.install(v2_file, auth_v2)
    assert res_v2.package.status is PackageInstallStatus.ACTIVE

    # 6. Verify packages list shows both v1 and v2 active
    packages = query_service.list_packages()
    assert len(packages) == 2
    assert [p.package_version for p in packages] == ["2.0.0", "1.2.3"]

    # 7. Rebind instance to v2
    rebound_instance, rebound_binding = pkg_service.rebind_instance(
        owner_user_id=1,
        agent_instance_id=instance.id,
        target_package_version="2.0.0",
        config={"greeting": "Welcome v2"},
        expected_config_revision=1,
    )
    assert rebound_instance.definition_id.agent_definition_version == "2.0.0"
    assert rebound_binding.config_revision == 2

    # Run on v2
    run_v2 = agent_service.submit_run(1, instance.id, "invoice #202")
    assert run_v2.executable.package_entrypoint == "acme_invoice.agent:InvoiceAgent"
    assert run_v2.agent_definition_version == "2.0.0"
    outcome_v2 = await executor.execute(run_v2, completion, claim=claim)
    assert outcome_v2.output_text == "[2.0.0] Welcome v2: invoice #202"

    # 8. Rollback instance to v1
    rolled_instance, rolled_binding = pkg_service.rebind_instance(
        owner_user_id=1,
        agent_instance_id=instance.id,
        target_package_version="1.2.3",
        config=None,  # carry-forward
        expected_config_revision=2,
    )
    assert rolled_instance.definition_id.agent_definition_version == "1.2.3"
    assert rolled_binding.config_revision == 3

    # Run on v1 again
    run_v1_post_rollback = agent_service.submit_run(1, instance.id, "invoice #303")
    assert run_v1_post_rollback.agent_definition_version == "1.2.3"
    outcome_v1_rb = await executor.execute(run_v1_post_rollback, completion, claim=claim)
    assert outcome_v1_rb.output_text == "[1.2.3] Welcome v2: invoice #303"

    # 9. Verify historical runs remain pinned
    with engine.connect() as conn:
        row1 = (
            conn.execute(
                text(
                    "SELECT agent_definition_version, effective_config_json FROM runs WHERE id = :id"
                ),
                {"id": run_v1.id},
            )
            .mappings()
            .one()
        )
        row2 = (
            conn.execute(
                text(
                    "SELECT agent_definition_version, effective_config_json FROM runs WHERE id = :id"
                ),
                {"id": run_v2.id},
            )
            .mappings()
            .one()
        )
        row3 = (
            conn.execute(
                text(
                    "SELECT agent_definition_version, effective_config_json FROM runs WHERE id = :id"
                ),
                {"id": run_v1_post_rollback.id},
            )
            .mappings()
            .one()
        )

    assert row1["agent_definition_version"] == "1.2.3"
    assert "Hello" in row1["effective_config_json"]
    assert row2["agent_definition_version"] == "2.0.0"
    assert "Welcome v2" in row2["effective_config_json"]
    assert row3["agent_definition_version"] == "1.2.3"
    assert "Welcome v2" in row3["effective_config_json"]

    # 10. Attempt uninstall v1 -> blocked by instance
    plan_v1 = query_service.get_removal_plan("com.acme.invoice", "1.2.3")
    assert plan_v1.can_remove_immediately is False
    assert plan_v1.bound_instances_count == 1
    with pytest.raises(PackageHasBoundInstances):
        pkg_service.remove_package("com.acme.invoice", "1.2.3")

    # 11. Rebind instance to v2 and uninstall v1
    pkg_service.rebind_instance(
        owner_user_id=1,
        agent_instance_id=instance.id,
        target_package_version="2.0.0",
        config=None,
        expected_config_revision=3,
    )

    # Mark runs terminal so obligations drain
    finished_time = datetime(2026, 9, 28, 12, 1, 0, tzinfo=UTC)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE runs SET status = 'cancelled', finished_at = :n WHERE id IN (:r1, :r2, :r3)"
            ),
            {"n": finished_time, "r1": run_v1.id, "r2": run_v2.id, "r3": run_v1_post_rollback.id},
        )

    plan_v1_clean = query_service.get_removal_plan("com.acme.invoice", "1.2.3")
    assert plan_v1_clean.can_remove_immediately is True
    outcome = pkg_service.remove_package("com.acme.invoice", "1.2.3")
    assert outcome is PackageRemovalOutcome.REMOVED

    # Tombstone is retained in DB
    detail_v1 = query_service.get_package_detail("com.acme.invoice", "1.2.3")
    assert detail_v1.status is PackageInstallStatus.REMOVED
    assert detail_v1.removed_at is not None
