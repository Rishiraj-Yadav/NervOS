"""Real Stage G acceptance through PackageApplicationService and the shipped Worker loop."""

# pyright: basic

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.genai import types
from nervos_core.application.agent_definitions import create_composite_agent_definition_resolver
from nervos_core.application.agents import AgentService
from nervos_core.application.builtin_tools import (
    builtin_tool_specs,
    create_builtin_tool_registry,
    reconcile_builtin_definitions,
)
from nervos_core.application.package_builder import PackageBuildInputs, package_build_bytes
from nervos_core.application.package_environment import PackageEnvironmentBuilder
from nervos_core.application.package_health import SubprocessPackageHealthChecker
from nervos_core.application.package_installation import PackageApplicationService
from nervos_core.application.package_query import PackageQueryService
from nervos_core.application.package_signing import Ed25519PackageSigner
from nervos_core.application.package_storage import PackageStore
from nervos_core.application.tool_invocation_mediator import ToolInvocationMediator
from nervos_core.domain.package_installation import PackageInstallAuthorization
from nervos_core.domain.runs import RunStatus
from nervos_core.infrastructure.database import create_session_factory
from nervos_core.infrastructure.database.agents import SqlAlchemyAgentPersistence
from nervos_core.infrastructure.database.jobs import SqlAlchemyJobPersistence
from nervos_core.infrastructure.database.packages import (
    SqlAlchemyPackageRegistryPersistence,
    SqlInstalledPackageDefinitionSource,
)
from nervos_core.infrastructure.database.tool_definitions import (
    SqlAlchemyToolDefinitionPersistence,
)
from nervos_core.infrastructure.database.tool_invocations import (
    SqlAlchemyToolInvocationPersistence,
)
from nervos_core.infrastructure.database.tools import (
    SqlAlchemyToolPermissionEvaluator,
    SqlAlchemyToolPermissionPersistence,
)
from nervos_models import compose_model_providers
from nervos_models.gemini import GeminiModelCompletion
from nervos_worker.package_execution import PackageExecutionAdapter
from sqlalchemy import text

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[4] / "packages" / "nervos-core" / "tests" / "unit"),
)
from package_fixtures import (  # pyright: ignore[reportMissingImports]
    TEST_SIGNING_SEED,
    VALID_CONFIG_SCHEMA,
    build_wheel_bytes,
)
from support import NOW, build_worker, migrate, run_until_stopped

ROOT = Path(__file__).resolve().parents[4]


@pytest.mark.anyio
async def test_real_package_runtime_runs_through_worker_and_tool_mediator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "g5-real-runtime.db", monkeypatch)
    try:
        package_store = PackageStore(tmp_path / "isolated-package-store")
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "prepare_runtime_artifacts.py"),
                "--destination",
                str(package_store.root / "runtime"),
            ],
            cwd=ROOT,
            check=True,
        )
        environment_builder = PackageEnvironmentBuilder(package_store.environments_root)
        assert environment_builder.runtime.sdk.path.parent == package_store.root / "runtime"

        package_registry = SqlAlchemyPackageRegistryPersistence(engine)
        manifest = b"""manifest_version: "1"
package_id: com.acme.runtime
package_name: acme-runtime
package_version: 1.0.0
publisher: NervOS test fixture
display_name: Runtime Acceptance Agent
runtime:
  language: python
  python: ">=3.12,<4"
  entrypoint: acme_invoice.agent:InvoiceAgent
nervos:
  min_version: 0.1.0
  max_version: 0.1.0
configuration:
  schema: config.schema.json
tools:
  required:
    - calculate
resources:
  limits:
    max_model_calls: 2
    max_tool_calls: 2
"""
        entrypoint = b"""from nervos_sdk import AgentResult, ModelMessage, ModelRequest, ToolRequest

class InvoiceAgent:
    async def run(self, context):
        assert context.model is not None
        answer = await context.model.complete(
            ModelRequest(
                messages=[ModelMessage(role="user", content=context.input["text"])],
                model="offline-test-model",
            )
        )
        result = await context.tools.invoke(
            ToolRequest(name="calculate", arguments={"expression": "19 + 23"})
        )
        if result.is_error:
            return AgentResult(final_message="stage-g-real-runtime-denied")
        value = result.content.get("structured", {}).get("result")
        return AgentResult(final_message=f"stage-g-real-runtime-ok:{value}:{answer.output_text}")
        """
        agent_wheel = build_wheel_bytes(members={"acme_invoice/agent.py": entrypoint})
        signer = Ed25519PackageSigner.from_private_bytes(TEST_SIGNING_SEED)
        archive = package_build_bytes(
            PackageBuildInputs(
                manifest_bytes=manifest,
                config_schema_bytes=VALID_CONFIG_SCHEMA,
                agent_wheel_bytes=agent_wheel,
            ),
            signer=signer,
        )
        staged_file = tmp_path / "runtime-acceptance.nervos"
        staged_file.write_bytes(archive)
        inspected = PackageQueryService(package_registry, package_store).inspect_artifact(
            staged_file
        )
        authorization = PackageInstallAuthorization(
            package_id=inspected.package_id,
            package_version=inspected.package_version,
            content_digest=inspected.content_digest,
            signer_fingerprint=inspected.signer_fingerprint,
            archive_digest=inspected.archive_digest,
            approved_by_user_id=1,
            approved_at=NOW,
        )
        package_service = PackageApplicationService(
            registry=package_registry,
            store=package_store,
            environment_builder=environment_builder,
            health_checker=SubprocessPackageHealthChecker(),
            clock=lambda: NOW,
        )
        installed = package_service.install(staged_file, authorization)
        assert installed.package.status.value == "active"

        instance, _binding = package_service.create_package_instance(
            owner_user_id=1,
            package_id="com.acme.runtime",
            package_version="1.0.0",
            display_name="Runtime Agent",
            model_provider="gemini",
            model_name="offline-test-model",
            config={},
        )

        definitions = SqlAlchemyToolDefinitionPersistence(engine)
        descriptors = reconcile_builtin_definitions(
            definitions,
            specs=builtin_tool_specs(clock=lambda: NOW),
            now=NOW,
        )
        calculate = next(item for item in descriptors if item.upstream_name == "calculate")
        SqlAlchemyToolPermissionPersistence(engine).grant_tool(
            owner_user_id=1,
            agent_instance_id=instance.id,
            tool_definition_id=calculate.tool_definition_id,
            now=NOW,
        )
        tool_registry = create_builtin_tool_registry(definitions, clock=lambda: NOW)
        tool_invocations = SqlAlchemyToolInvocationPersistence(engine)
        mediator = ToolInvocationMediator(
            registry=tool_registry,
            authorize=SqlAlchemyToolPermissionEvaluator(engine),
            invocations=tool_invocations,
            clock=lambda: NOW,
        )
        adapter = PackageExecutionAdapter(
            package_store.root,
            mediator=mediator,
            registry=tool_registry,
            packages=package_registry,
        )

        resolver = create_composite_agent_definition_resolver(
            [SqlInstalledPackageDefinitionSource(engine)]
        )

        class FakeGeminiModels:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            async def generate_content(self, **kwargs: object) -> types.GenerateContentResponse:
                self.calls.append(kwargs)
                return types.GenerateContentResponse(
                    candidates=[
                        types.Candidate(
                            content=types.Content(
                                role="model", parts=[types.Part(text="Gemini package answer")]
                            ),
                            finish_reason=types.FinishReason.STOP,
                        )
                    ]
                )

        fake_models = FakeGeminiModels()
        completion = GeminiModelCompletion(SimpleNamespace(aio=SimpleNamespace(models=fake_models)))  # type: ignore[arg-type]
        agent_service = AgentService(
            SqlAlchemyAgentPersistence(create_session_factory(engine)),
            resolver,
            lambda: NOW,
            compose_model_providers(None, None).catalog,
            SqlAlchemyJobPersistence(engine),
        )
        run = agent_service.submit_run(1, instance.id, "calculate the fixture value")
        assert run.executable.installed_package_version_id == installed.package.id
        assert run.executable.package_environment_digest is not None
        worker = build_worker(
            engine,
            {"gemini": completion},
            package_execution=adapter,
        )
        await run_until_stopped(worker, engine)

        result = agent_service.get_run(1, run.id)
        assert result.status is RunStatus.SUCCEEDED
        assert result.output_text == "stage-g-real-runtime-ok:42:Gemini package answer"
        assert len(fake_models.calls) == 1
        assert fake_models.calls[0]["model"] == "offline-test-model"
        with engine.connect() as connection:
            invocation = connection.execute(
                text("SELECT upstream_name, status FROM tool_invocations WHERE run_id=:run_id"),
                {"run_id": run.id},
            ).one()
        assert invocation == ("calculate", "succeeded")

        # Installation and a signed declaration do not create a grant. Revoke the explicit grant
        # and drive a second Run through the same real package-host subprocess; the package receives
        # a bounded denial, while the shared Stage-D ledger records denied and no executor starts.
        SqlAlchemyToolPermissionPersistence(engine).revoke_tool(
            owner_user_id=1,
            agent_instance_id=instance.id,
            tool_definition_id=calculate.tool_definition_id,
        )
        denied_run = agent_service.submit_run(1, instance.id, "request the declared tool again")
        denied_worker = build_worker(
            engine,
            {"gemini": completion},
            worker_id="worker-2",
            package_execution=adapter,
        )
        await run_until_stopped(denied_worker, engine)
        denied_result = agent_service.get_run(1, denied_run.id)
        assert denied_result.status is RunStatus.SUCCEEDED
        assert denied_result.output_text == "stage-g-real-runtime-denied"
        assert len(fake_models.calls) == 2
        with engine.connect() as connection:
            denied_invocation = connection.execute(
                text(
                    "SELECT upstream_name, status, started_at FROM tool_invocations "
                    "WHERE run_id=:run_id"
                ),
                {"run_id": denied_run.id},
            ).one()
        assert denied_invocation == ("calculate", "denied", None)
    finally:
        engine.dispose()
