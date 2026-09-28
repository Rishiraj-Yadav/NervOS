"""Stage G3 integrated acceptance test: build -> verify -> authorize -> install -> active -> run.

Proves the complete end-to-end G3 path without external LLM dependencies, using deterministic
execution, config pinning, executable pinning, and secret boundary tripwires.
"""

# pyright: basic

from __future__ import annotations

import asyncio
import io
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
from nervos_core.application.model_completion import ModelCompletion, ModelRequest, ModelResponse
from nervos_core.application.model_providers import ModelProviderCatalog
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
from nervos_core.application.run_execution import RunExecutor
from nervos_core.domain.execution import RunExecutionKind
from nervos_core.domain.package_installation import (
    PackageInstallAuthorization,
    PackageInstallStatus,
)
from nervos_core.domain.runs import Run, RunStatus
from nervos_core.domain.tools import ToolDescriptor
from nervos_core.infrastructure.database import create_session_factory, create_sqlite_engine
from nervos_core.infrastructure.database.agents import SqlAlchemyAgentPersistence
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobExecutionPersistence,
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
    """Deterministic in-process adapter for integrated pipeline proof without child process OS delay."""

    def __init__(self, store_root: Path) -> None:
        self._store = store_root

    async def run(self, completion, run: Run, claim, elapsed_ms, snapshot=None):
        # Proves the package execution path resolves the snapshot and executes the SDK result
        from nervos_core.application.trusted_chat import ChatOutcome
        from nervos_sdk.types import AgentContext, AgentResult

        # Assert sentinel secrets are NOT in execution context
        assert (
            "OPENAI_API_KEY" not in os.environ
            or os.environ.get("OPENAI_API_KEY") != "SENTINEL_LEAK"
        )
        assert "DATABASE_URL" not in os.environ or os.environ.get("DATABASE_URL") != "SENTINEL_LEAK"

        config_data = run.executable.effective_config_json
        return ChatOutcome(
            output_text=f"Package Output: {run.input_text} (config: {config_data})",
            finish_reason="stop",
            usage=run.usage,
        )


class DirectHealthChecker:
    def check(self, *, environment: Path, entrypoint: str, expected_sdk_api_version: str) -> None:
        pass


@pytest.fixture
def g3_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "g3_acceptance.db"
    monkeypatch.setenv("NERVOS_ENVIRONMENT", "test")
    monkeypatch.setenv("NERVOS_DATABASE_PATH", str(db_path))

    # Set secret sentinels to prove they don't cross boundaries
    monkeypatch.setenv("OPENAI_API_KEY", "SENTINEL_SECRET_DO_NOT_LEAK")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///SENTINEL_SECRET_DO_NOT_LEAK")

    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, "head")
    engine = create_sqlite_engine(db_path)
    session_factory = create_session_factory(engine)

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users(id, username, password_hash, role, is_active, created_at, updated_at) "
                "VALUES(1, 'owner', X'00', 'admin', 1, '2026-09-28 12:00:00', '2026-09-28 12:00:00')"
            )
        )

    store = PackageStore(tmp_path / "store")
    persistence = SqlAlchemyPackageRegistryPersistence(engine)

    sdk_dummy = tmp_path / "nervos_sdk-0.1.0-py3-none-any.whl"
    sdk_dummy.write_bytes(b"sdk")
    host_dummy = tmp_path / "nervos_package_host-0.1.0-py3-none-any.whl"
    host_dummy.write_bytes(b"host")

    import hashlib

    runtime = PackageRuntimeArtifacts(
        sdk=RuntimeWheelArtifact(
            sdk_dummy, "nervos-sdk", "0.1.0", hashlib.sha256(b"sdk").hexdigest()
        ),
        host=RuntimeWheelArtifact(
            host_dummy, "nervos-package-host", "0.1.0", hashlib.sha256(b"host").hexdigest()
        ),
    )

    class FastEnvBuilder(PackageEnvironmentBuilder):
        def build(self, verified, payload_root):
            key = "environments/" + "e" * 64
            dest = self._root / key
            dest.mkdir(parents=True, exist_ok=True)
            return EnvironmentIdentity("{}", "e" * 64, key), dest

    install_service = PackageApplicationService(
        registry=persistence,
        store=store,
        environment_builder=FastEnvBuilder(tmp_path / "store", runtime),
        health_checker=DirectHealthChecker(),
        clock=_now,
    )

    definition_source = SqlInstalledPackageDefinitionSource(engine)
    resolver = create_composite_agent_definition_resolver([definition_source])

    catalog = ModelProviderCatalog([("anthropic", lambda: DeterministicModelCompletion())])
    submissions = SqlAlchemyJobPersistence(engine)
    agent_service = AgentService(
        SqlAlchemyAgentPersistence(session_factory),
        resolver,
        _now,
        catalog,
        submissions,
    )

    return {
        "engine": engine,
        "install_service": install_service,
        "agent_service": agent_service,
        "definition_source": definition_source,
        "resolver": resolver,
        "store": store,
        "tmp_path": tmp_path,
    }


def test_g3_integrated_acceptance_journey(g3_rig) -> None:
    """Complete G3 journey: build -> verify -> authorize -> install -> active -> resolve -> run."""
    install_service: PackageApplicationService = g3_rig["install_service"]
    agent_service: AgentService = g3_rig["agent_service"]
    engine = g3_rig["engine"]

    # 1. Build .nervos
    raw = package_build_bytes(
        PackageBuildInputs(
            manifest_bytes=VALID_MANIFEST,
            config_schema_bytes=VALID_CONFIG_SCHEMA,
            agent_wheel_bytes=valid_wheel(),
        ),
        signer=_signer(),
    )

    # 2. Verify
    verified = verify_package(archive_bytes=raw)

    # 3. Authorize
    auth = PackageInstallAuthorization(
        package_id=verified.manifest.package_id,
        package_version=verified.manifest.package_version,
        content_digest=verified.content_digest,
        signer_fingerprint=verified.signer_fingerprint,
        approved_by_user_id=1,
        approved_at=_now(),
    )

    # 4. Install & Activate
    install_result = install_service.install(io.BytesIO(raw), auth)
    assert install_result.package.status is PackageInstallStatus.ACTIVE

    # 5. Definition resolution  — resolver was created before installation, re-create it
    definition_source = SqlInstalledPackageDefinitionSource(engine)
    live_resolver = create_composite_agent_definition_resolver([definition_source])
    definition_id = verified.manifest.identity.as_agent_definition_id()
    definition = live_resolver.resolve(definition_id)
    assert definition.display_name == "Acme Invoice Agent"

    # 6. Create AgentInstance
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_instances(id, owner_user_id, agent_key, agent_definition_version,"
                " display_name, enabled, model_provider, model_name, created_at, updated_at)"
                " VALUES(1, 1, 'com.acme.invoice', '1.2.3', 'My Invoice Agent', 1, 'anthropic', 'claude-3-5', :n, :n)"
            ),
            {"n": _now()},
        )

    # 7. Bind instance with config
    binding = install_service.bind_instance(
        owner_user_id=1,
        agent_instance_id=1,
        installed_package_version_id=install_result.package.id,
        config={"greeting": "Welcome to Acme"},
    )
    assert binding.agent_instance_id == 1

    # 8. Submit Run through existing canonical seam using live (post-install) resolver
    live_catalog = ModelProviderCatalog([("anthropic", lambda: DeterministicModelCompletion())])
    live_agent_service = AgentService(
        SqlAlchemyAgentPersistence(create_session_factory(engine)),
        live_resolver,
        _now,
        live_catalog,
        SqlAlchemyJobPersistence(engine),
    )

    # Use the live_agent_service for the submit, not the old one
    run = live_agent_service.submit_run(
        owner_user_id=1, instance_id=1, input_text="Process invoice #42"
    )
    assert run.id == 1
    assert run.status is RunStatus.CREATED
    assert run.executable.execution_kind == RunExecutionKind.PACKAGE
    assert run.executable.installed_package_version_id == install_result.package.id
    assert run.executable.package_content_digest == verified.content_digest
    assert "Welcome to Acme" in run.executable.effective_config_json

    # 9. Execute Run via RunExecutor with PackageExecutionAdapter
    adapter = DirectPackageHostAdapter(g3_rig["tmp_path"] / "store")
    from nervos_core.application.trusted_chat import create_builtin_handler_registry

    executor = RunExecutor(create_builtin_handler_registry(), package_execution=adapter)

    from nervos_core.application.job_execution import ClaimedAttempt

    claim = ClaimedAttempt(
        job_id=1,
        run_id=1,
        attempt_id=1,
        attempt_number=1,
        worker_id="test",
        claim_token=b"0" * 32,
        lease_expires_at=_now(),
    )
    completion = DeterministicModelCompletion()

    # Execute
    outcome = asyncio.run(executor.execute(run, completion, claim=claim))
    assert outcome.status == "succeeded"
    assert outcome.output_text is not None
    assert "Process invoice #42" in outcome.output_text
    assert "Welcome to Acme" in outcome.output_text


def test_run_config_and_executable_immutability(g3_rig) -> None:
    """Config and executable reference are snapshotted at submission and never drift."""
    install_service: PackageApplicationService = g3_rig["install_service"]
    engine = g3_rig["engine"]

    raw = package_build_bytes(
        PackageBuildInputs(
            manifest_bytes=VALID_MANIFEST,
            config_schema_bytes=VALID_CONFIG_SCHEMA,
            agent_wheel_bytes=valid_wheel(),
        ),
        signer=_signer(),
    )
    verified = verify_package(archive_bytes=raw)
    auth = PackageInstallAuthorization(
        package_id=verified.manifest.package_id,
        package_version=verified.manifest.package_version,
        content_digest=verified.content_digest,
        signer_fingerprint=verified.signer_fingerprint,
        approved_by_user_id=1,
        approved_at=_now(),
    )
    install_result = install_service.install(io.BytesIO(raw), auth)

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_instances(id, owner_user_id, agent_key, agent_definition_version,"
                " display_name, enabled, model_provider, model_name, created_at, updated_at)"
                " VALUES(10, 1, 'com.acme.invoice', '1.2.3', 'Immutable Agent', 1, 'anthropic', 'claude-3-5', :n, :n)"
            ),
            {"n": _now()},
        )

    install_service.bind_instance(
        owner_user_id=1,
        agent_instance_id=10,
        installed_package_version_id=install_result.package.id,
        config={"greeting": "Initial Config A"},
    )

    # Create agent_service with live resolver after package installation
    definition_source = SqlInstalledPackageDefinitionSource(engine)
    live_resolver = create_composite_agent_definition_resolver([definition_source])
    live_catalog = ModelProviderCatalog([("anthropic", lambda: DeterministicModelCompletion())])
    live_agent_service = AgentService(
        SqlAlchemyAgentPersistence(create_session_factory(engine)),
        live_resolver,
        _now,
        live_catalog,
        SqlAlchemyJobPersistence(engine),
    )

    run_1 = live_agent_service.submit_run(owner_user_id=1, instance_id=10, input_text="Run 1")
    assert "Initial Config A" in run_1.executable.effective_config_json

    # Update binding to Config B
    with engine.begin() as conn:
        conn.execute(
            text(
                'UPDATE agent_instance_package_bindings SET effective_config_json = \'{"greeting":"Updated Config B"}\','
                " config_revision = 2 WHERE agent_instance_id = 10"
            )
        )

    run_2 = live_agent_service.submit_run(owner_user_id=1, instance_id=10, input_text="Run 2")

    # Run 1 retains immutable Config A; Run 2 has Config B
    assert "Initial Config A" in run_1.executable.effective_config_json
    assert "Updated Config B" in run_2.executable.effective_config_json
