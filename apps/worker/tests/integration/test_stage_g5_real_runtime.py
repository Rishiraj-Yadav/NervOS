"""Real Stage G acceptance through PackageApplicationService and the shipped Worker loop."""

# pyright: basic

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import types
from nervos_core.application.agent_definitions import create_composite_agent_definition_resolver
from nervos_core.application.agents import AgentService
from nervos_core.application.builtin_tools import (
    builtin_tool_specs,
    create_builtin_tool_registry,
    reconcile_builtin_definitions,
)
from nervos_core.application.mcp_connections import ConnectionTransport
from nervos_core.application.package_builder import PackageBuildInputs, package_build_bytes
from nervos_core.application.package_environment import PackageEnvironmentBuilder
from nervos_core.application.package_health import SubprocessPackageHealthChecker
from nervos_core.application.package_installation import PackageApplicationService
from nervos_core.application.package_query import PackageQueryService
from nervos_core.application.package_signing import Ed25519PackageSigner
from nervos_core.application.package_storage import PackageStore
from nervos_core.application.sandbox import PackageContainment
from nervos_core.application.tool_invocation_mediator import ToolInvocationMediator
from nervos_core.application.tool_registry import ToolResult
from nervos_core.domain.memory import (
    MemoryProvenanceType,
    MemoryScope,
    MemorySourceKind,
    compute_memory_digest,
)
from nervos_core.domain.package_installation import PackageInstallAuthorization
from nervos_core.domain.runs import RunStatus
from nervos_core.domain.tools import (
    DefinitionStatus,
    ToolSourceKind,
    ToolSourceRef,
    definition_fingerprint,
)
from nervos_core.infrastructure.database import create_session_factory
from nervos_core.infrastructure.database.agents import SqlAlchemyAgentPersistence
from nervos_core.infrastructure.database.jobs import SqlAlchemyJobPersistence
from nervos_core.infrastructure.database.mcp_connections import SqlAlchemyMcpConnectionPersistence
from nervos_core.infrastructure.database.memory import SqlAlchemyMemoryPersistence
from nervos_core.infrastructure.database.packages import (
    SqlAlchemyPackageRegistryPersistence,
    SqlInstalledPackageDefinitionSource,
)
from nervos_core.infrastructure.database.runtime_integration import (
    SqlAlchemyRuntimeIntegrationPersistence,
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


def trusted_fixture_resources():
    """Stage-G fixture evidence only; not H4 filesystem/network qualification."""
    if sys.platform == "win32":
        from nervos_core.infrastructure.sandbox.windows import WindowsJobObjectContainment

        return WindowsJobObjectContainment()
    from nervos_core.infrastructure.sandbox.posix import PosixContainment

    return PosixContainment()


async def _real_runtime_journey(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    structured_mcp: bool,
    *,
    containment_factory: Callable[[], PackageContainment],
    health_checker_factory: Callable[[], SubprocessPackageHealthChecker],
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
        integrations = SqlAlchemyRuntimeIntegrationPersistence(engine)
        if structured_mcp:
            spec = next(
                s for s in builtin_tool_specs(clock=lambda: NOW) if s.upstream_name == "calculate"
            )
            schema_text = json.dumps(
                spec.input_schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            schema_digest = hashlib.sha256(schema_text.encode()).hexdigest()
            manifest = manifest.replace(b"tools:\n  required:\n    - calculate\n", b"")
            manifest += (
                "x-nervos-runtime-integration:\n"
                "  version: 1\n"
                "  context: structured-v1\n"
                "  mcp_tools:\n"
                "    - alias: research.calculate\n"
                "      upstream_name: calculate\n"
                "      input_schema_sha256: " + schema_digest + "\n      required: true\n"
            ).encode()
            entrypoint = entrypoint.replace(
                b"AgentResult, ModelMessage", b"AgentResult, MemoryProposal, ModelMessage"
            )
            entrypoint = entrypoint.replace(
                b"        assert context.model is not None",
                b"        assert context.model is not None\n"
                b'        assert context.input["text"] in '
                b'("calculate the fixture value", "request the declared tool again")\n'
                b'        if context.input["text"] == "calculate the fixture value":\n'
                b'            assert context.memory[0].content == "User prefers concise answers."\n'
                b"            assert context.memory[0].version == 1",
            )
            entrypoint = entrypoint.replace(
                b'messages=[ModelMessage(role="user", content=context.input["text"])],\n'
                b'                model="offline-test-model",',
                b'messages=[ModelMessage(role="system", content="Package instructions"), '
                b'ModelMessage(role="user", content="first question"), '
                b'ModelMessage(role="assistant", content="first answer"), '
                b'ModelMessage(role="user", content=context.input["text"])],',
            )
            entrypoint = entrypoint.replace(b'name="calculate"', b'name="research.calculate"')
            entrypoint = entrypoint.replace(
                b'AgentResult(final_message=f"stage-g-real-runtime-ok:42',
                b'AgentResult(final_message=f"stage-g-real-runtime-ok:42',
            )
            entrypoint = entrypoint.replace(
                b'AgentResult(final_message=f"stage-g-real-runtime-ok:{value}:{answer.output_text}")',
                (
                    b'AgentResult(final_message=f"stage-g-real-runtime-ok:{value}:'
                    b'{answer.output_text}", '
                    b'memory_proposals=[MemoryProposal(content="User prefers cited research.")])'
                ),
            )
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
            health_checker=health_checker_factory(),
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
        if structured_mcp:
            connections = SqlAlchemyMcpConnectionPersistence(engine)
            connection_id = connections.create(
                owner_user_id=1,
                display_name="Approved fixture",
                transport=ConnectionTransport.STDIO,
                endpoint=None,
                server_key="fixture",
                credential_ref=None,
                now=NOW,
            )
            with engine.begin() as conn:
                conn.execute(
                    text("UPDATE mcp_connections SET catalog_status='connected' WHERE id=:id"),
                    {"id": connection_id},
                )
            original = definitions.find_builtin(upstream_name="calculate")
            assert original is not None
            material = replace(
                original.material,
                source_kind=ToolSourceKind.MCP,
                source_id=connection_id,
                model_name="nervos__mcp__fixture_calculate",
            )
            material = replace(
                material,
                fingerprint=definition_fingerprint(
                    model_name=material.model_name,
                    upstream_name=material.upstream_name,
                    description=material.description,
                    input_schema=spec.input_schema,
                    output_schema=spec.output_schema,
                    source_kind=ToolSourceKind.MCP,
                    source_id=connection_id,
                    risk_hints=material.risk_hints,
                ),
                status=DefinitionStatus.AVAILABLE,
            )
            definition_id = definitions.insert(material, now=NOW)
            calculate = replace(
                original, tool_definition_id=definition_id, material=material
            ).to_descriptor()
            integrations.bind_tool(1, instance.id, "research.calculate", definition_id, NOW)
            integrations.set_policy(1, instance.id, "automatic_private", False, 0, NOW)
            SqlAlchemyMemoryPersistence(engine).create_memory(
                owner_user_id=1,
                scope=MemoryScope.AGENT,
                agent_instance_id=instance.id,
                content="User prefers concise answers.",
                content_digest=compute_memory_digest("User prefers concise answers."),
                source_kind=MemorySourceKind.DIRECT_USER,
                source_id=None,
                provenance_type=MemoryProvenanceType.USER_AUTHORED,
                now=NOW,
            )
        SqlAlchemyToolPermissionPersistence(engine).grant_tool(
            owner_user_id=1,
            agent_instance_id=instance.id,
            tool_definition_id=calculate.tool_definition_id,
            now=NOW,
        )
        tool_registry = create_builtin_tool_registry(definitions, clock=lambda: NOW)
        if structured_mcp:

            class FixtureMcpSource:
                async def list_tools(self):
                    return (calculate,)

            class FixtureMcpExecutor:
                async def execute(self, descriptor, arguments):
                    assert descriptor.tool_definition_id == calculate.tool_definition_id
                    return ToolResult(text="42", structured={"result": 42})

            tool_registry.register(
                source_ref=ToolSourceRef(ToolSourceKind.MCP, connection_id),
                source=FixtureMcpSource(),
                executor=FixtureMcpExecutor(),
            )
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
            integrations=integrations,
            containment_factory=containment_factory,
        )

        resolver = create_composite_agent_definition_resolver(
            [SqlInstalledPackageDefinitionSource(engine)]
        )

        class FakeGeminiModels:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

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

        if structured_mcp:
            integrations.maintain_memory(NOW)
            assert integrations.suggestions(1, instance.id)["items"][0]["state"] == "saved"
            assert len(fake_models.calls[0]["contents"]) == 3
            assert [item.role for item in fake_models.calls[0]["contents"]] == [
                "user",
                "model",
                "user",
            ]
            assert (
                fake_models.calls[0]["contents"][-1].parts[0].text == "calculate the fixture value"
            )
            assert (
                sum(
                    part.text.count("User prefers concise answers.")
                    for message in fake_models.calls[0]["contents"]
                    for part in message.parts
                    if part.text
                )
                == 1
            )

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
        assert denied_result.status is RunStatus.SUCCEEDED, (
            denied_result.error_code,
            denied_result.error_message,
        )
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


@pytest.mark.anyio
@pytest.mark.parametrize("structured_mcp", [False, True])
async def test_real_package_runtime_runs_through_worker_and_tool_mediator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, structured_mcp: bool
) -> None:
    await _real_runtime_journey(
        tmp_path,
        monkeypatch,
        structured_mcp,
        containment_factory=trusted_fixture_resources,
        health_checker_factory=lambda: SubprocessPackageHealthChecker(
            authorize_launch=lambda: None
        ),
    )


LINUX_BWRAP = platform.system() == "Linux" and shutil.which("bwrap") is not None


@pytest.mark.skipif(
    not LINUX_BWRAP,
    reason="qualified isolation requires a Linux kernel with bubblewrap installed",
)
@pytest.mark.anyio
async def test_real_package_runtime_runs_under_qualified_linux_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same signed-package journey through the production launcher (ADR 0038).

    Unlike the fixture-resource variant above, this test never injects a double: the
    installation health check and the Worker package host both start through
    ``create_containment()``, so passing it proves the qualified Linux path end to end.
    """
    from nervos_core.application.package_health import QualifiedPackageHealthChecker
    from nervos_core.infrastructure.sandbox import create_containment

    await _real_runtime_journey(
        tmp_path,
        monkeypatch,
        True,
        containment_factory=create_containment,
        health_checker_factory=lambda: QualifiedPackageHealthChecker(create_containment),
    )
