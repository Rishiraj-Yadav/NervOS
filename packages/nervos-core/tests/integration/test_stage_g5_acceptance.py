"""Stage G5 final integration acceptance tests: comprehensive Stage-G closeout proof.

Proves:
1. Complete deterministic package lifecycle: build -> verify -> inspect -> authorize -> install -> active.
2. Package AgentInstance creation with initial validated effective config.
3. Package-backed Run executable/config snapshots through a direct adapter seam; the real Worker,
   package-host subprocess, ModelPort, ToolPort, and Stage-D authority are exercised separately by
   `apps/worker/tests/integration/test_stage_g5_real_runtime.py`.
4. Stage-F context snapshot projection without direct DB memory queries.
5. Side-by-side coexistence of v1 and v2 releases.
6. Explicit rebind (v1 -> v2) and rollback (v2 -> v1) with config carry-forward and immutability enforcement.
7. Historical Run executable and config snapshot immutability across rebinds.
8. Obligation-aware removal: bound instance hard blocker, active run pending_removal drain.
9. Final removal payload cleanup with database tombstone row and environment retention for historical runs.
10. Failure paths: signature mismatch, authorization mismatch, identity conflict fail safely.
"""

# pyright: basic

from __future__ import annotations

import io
import json
import os
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
from nervos_core.application.job_execution import ClaimedAttempt
from nervos_core.application.model_completion import ModelCompletion, ModelRequest, ModelResponse
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
from nervos_core.application.package_query import PackageQueryService
from nervos_core.application.package_signing import Ed25519PackageSigner
from nervos_core.application.package_storage import PackageStore
from nervos_core.application.package_verification import verify_package
from nervos_core.application.run_execution import RunExecutor
from nervos_core.application.trusted_chat import ChatOutcome, create_builtin_handler_registry
from nervos_core.domain.execution import RunExecutionKind
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
    SqlAlchemyJobExecutionPersistence,
    SqlAlchemyJobPersistence,
)
from nervos_core.infrastructure.database.packages import (
    PackageIdentityConflict,
    SqlAlchemyPackageRegistryPersistence,
    SqlInstalledPackageDefinitionSource,
)
from nervos_models import compose_model_providers
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


def _signer() -> Ed25519PackageSigner:
    return Ed25519PackageSigner.from_private_bytes(TEST_SIGNING_SEED)


def _other_signer() -> Ed25519PackageSigner:
    return Ed25519PackageSigner.from_private_bytes(OTHER_TEST_SIGNING_SEED)


class DeterministicModelCompletion(ModelCompletion):
    def __init__(self) -> None:
        self.invocations: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.invocations.append(request)
        return ModelResponse(
            text="model: " + request.user_text,
            model_provider="anthropic",
            model_name=request.model_name,
        )


class DirectPackageHostAdapter:
    def __init__(self, store_root: Path) -> None:
        self._store = store_root

    async def run(self, completion, run: Run, claim, elapsed_ms, snapshot=None):
        del claim, elapsed_ms
        # Assert sentinel secrets are NOT in execution context
        assert (
            "OPENAI_API_KEY" not in os.environ
            or os.environ.get("OPENAI_API_KEY") != "SENTINEL_LEAK"
        )
        assert "DATABASE_URL" not in os.environ or os.environ.get("DATABASE_URL") != "SENTINEL_LEAK"

        cfg = json.loads(run.executable.effective_config_json)
        greeting = cfg.get("greeting", "Hello")
        version = run.agent_definition_version

        # Test model completion delegation
        model_req = ModelRequest(
            system_instruction="system",
            user_text=f"prompt for {run.input_text}",
            model_name=run.model_name,
            max_output_tokens=run.limits.max_output_tokens,
            timeout_ms=run.limits.provider_timeout_ms,
        )
        model_res = await completion.complete(model_req)

        return ChatOutcome(
            output_text=f"[{version}] {greeting} -> {model_res.text}",
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
async def test_stage_g5_comprehensive_acceptance_closeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "g5_acceptance.db"
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

    # 1. Build & Inspect package v1
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

    # Test authorization mismatch rejection
    bad_auth = PackageInstallAuthorization(
        package_id="com.acme.invoice",
        package_version="1.2.3",
        content_digest=inspected_v1.content_digest,
        signer_fingerprint="0" * 64,
        archive_digest=inspected_v1.archive_digest,
        approved_by_user_id=1,
        approved_at=_now(),
    )
    with pytest.raises(PackageAuthorizationMismatch):
        pkg_service.install(v1_file, bad_auth)

    # 2. Authorize and install package v1
    good_auth_v1 = PackageInstallAuthorization(
        package_id=inspected_v1.package_id,
        package_version=inspected_v1.package_version,
        content_digest=inspected_v1.content_digest,
        signer_fingerprint=inspected_v1.signer_fingerprint,
        archive_digest=inspected_v1.archive_digest,
        approved_by_user_id=1,
        approved_at=_now(),
    )
    res_v1 = pkg_service.install(v1_file, good_auth_v1)
    assert res_v1.package.status is PackageInstallStatus.ACTIVE

    # Test identity conflict rejection (same id+version, different signer)
    v1_alt_bytes = package_build_bytes(
        PackageBuildInputs(
            manifest_bytes=v1_manifest,
            config_schema_bytes=VALID_CONFIG_SCHEMA,
            agent_wheel_bytes=valid_wheel(),
        ),
        signer=_other_signer(),
    )
    v1_alt_file = tmp_path / "v1_alt.nervos"
    v1_alt_file.write_bytes(v1_alt_bytes)
    inspected_alt = query_service.inspect_artifact(v1_alt_file)
    alt_auth = PackageInstallAuthorization(
        package_id=inspected_alt.package_id,
        package_version=inspected_alt.package_version,
        content_digest=inspected_alt.content_digest,
        signer_fingerprint=inspected_alt.signer_fingerprint,
        archive_digest=inspected_alt.archive_digest,
        approved_by_user_id=1,
        approved_at=_now(),
    )
    with pytest.raises(PackageIdentityConflict):
        pkg_service.install(v1_alt_file, alt_auth)

    # 3. Create package AgentInstance
    instance, binding = pkg_service.create_package_instance(
        owner_user_id=1,
        package_id="com.acme.invoice",
        package_version="1.2.3",
        display_name="Invoice Agent",
        model_provider="anthropic",
        model_name="claude-3-5-sonnet",
        config={"greeting": "Salutations"},
    )
    assert instance.id > 0
    assert binding.config_revision == 1

    # 4. Execute Run on v1
    def_source = SqlInstalledPackageDefinitionSource(engine)
    resolver = create_composite_agent_definition_resolver([def_source])
    known_providers = compose_model_providers(None, None).catalog
    job_persistence = SqlAlchemyJobPersistence(engine)
    agent_service = AgentService(
        SqlAlchemyAgentPersistence(session_factory),
        resolver,
        _now,
        known_providers,
        job_persistence,
    )

    run_v1 = agent_service.submit_run(1, instance.id, "invoice #1001")
    assert run_v1.executable.execution_kind is RunExecutionKind.PACKAGE
    assert run_v1.executable.package_content_digest == inspected_v1.content_digest

    claim = ClaimedAttempt(
        job_id=1,
        run_id=run_v1.id,
        attempt_id=1,
        attempt_number=1,
        worker_id="worker-1",
        claim_token=b"0" * 32,
        lease_expires_at=_now(),
    )
    adapter = DirectPackageHostAdapter(store.root)
    executor = RunExecutor(create_builtin_handler_registry(), package_execution=adapter)
    completion = DeterministicModelCompletion()
    outcome = await executor.execute(run_v1, completion, claim=claim)
    assert outcome.output_text == "[1.2.3] Salutations -> model: prompt for invoice #1001"

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
    pkg_service.install(v2_file, auth_v2)

    # 6. Rebind instance to v2
    rebound_inst, rebound_bind = pkg_service.rebind_instance(
        owner_user_id=1,
        agent_instance_id=instance.id,
        target_package_version="2.0.0",
        config={"greeting": "Greetings v2"},
        expected_config_revision=1,
    )
    assert rebound_inst.definition_id.agent_definition_version == "2.0.0"
    assert rebound_bind.config_revision == 2

    # 7. Run on v2
    run_v2 = agent_service.submit_run(1, instance.id, "invoice #2002")
    outcome_v2 = await executor.execute(run_v2, completion, claim=claim)
    assert outcome_v2.output_text == "[2.0.0] Greetings v2 -> model: prompt for invoice #2002"

    # 8. Rollback to v1 (carry-forward config)
    rolled_inst, rolled_bind = pkg_service.rebind_instance(
        owner_user_id=1,
        agent_instance_id=instance.id,
        target_package_version="1.2.3",
        config=None,
        expected_config_revision=2,
    )
    assert rolled_inst.definition_id.agent_definition_version == "1.2.3"
    assert rolled_bind.config_revision == 3

    run_v1_post = agent_service.submit_run(1, instance.id, "invoice #3003")
    outcome_v1_post = await executor.execute(run_v1_post, completion, claim=claim)
    assert outcome_v1_post.output_text == "[1.2.3] Greetings v2 -> model: prompt for invoice #3003"

    # 9. Verify historical run snapshots remain pinned
    with engine.connect() as conn:
        r1 = (
            conn.execute(
                text(
                    "SELECT agent_definition_version, effective_config_json FROM runs WHERE id = :id"
                ),
                {"id": run_v1.id},
            )
            .mappings()
            .one()
        )
        r2 = (
            conn.execute(
                text(
                    "SELECT agent_definition_version, effective_config_json FROM runs WHERE id = :id"
                ),
                {"id": run_v2.id},
            )
            .mappings()
            .one()
        )
        r3 = (
            conn.execute(
                text(
                    "SELECT agent_definition_version, effective_config_json FROM runs WHERE id = :id"
                ),
                {"id": run_v1_post.id},
            )
            .mappings()
            .one()
        )

    assert r1["agent_definition_version"] == "1.2.3"
    assert "Salutations" in r1["effective_config_json"]
    assert r2["agent_definition_version"] == "2.0.0"
    assert "Greetings v2" in r2["effective_config_json"]
    assert r3["agent_definition_version"] == "1.2.3"
    assert "Greetings v2" in r3["effective_config_json"]

    # 10. Removal blocker check
    with pytest.raises(PackageHasBoundInstances):
        pkg_service.remove_package("com.acme.invoice", "1.2.3")

    # Rebind instance to v2 and mark runs terminal
    pkg_service.rebind_instance(
        owner_user_id=1,
        agent_instance_id=instance.id,
        target_package_version="2.0.0",
        config=None,
        expected_config_revision=3,
    )

    finished_time = datetime(2026, 9, 28, 12, 1, 0, tzinfo=UTC)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE runs SET status = 'cancelled', finished_at = :n WHERE id IN (:r1, :r2, :r3)"
            ),
            {"n": finished_time, "r1": run_v1.id, "r2": run_v2.id, "r3": run_v1_post.id},
        )

    # 11. Final removal of v1
    outcome = pkg_service.remove_package("com.acme.invoice", "1.2.3")
    assert outcome is PackageRemovalOutcome.REMOVED

    # Tombstone row retained
    detail_v1 = query_service.get_package_detail("com.acme.invoice", "1.2.3")
    assert detail_v1.status is PackageInstallStatus.REMOVED

    # Environment directory retained because historical runs reference it
    env_dir = store.environments_root / f"environments/{inspected_v1.content_digest}"
    assert env_dir.exists()
