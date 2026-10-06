"""Actual signed demos -> isolated host -> ports -> fenced checkpoints -> Scheduler."""

# pyright: basic
import hashlib
import json
import platform
import runpy
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from nervos_core.application.agent_definitions import create_composite_agent_definition_resolver
from nervos_core.application.builtin_tools import BUILTIN_SOURCE_REF, create_builtin_tool_registry
from nervos_core.application.package_builder import PackageBuildInputs, package_build_bytes
from nervos_core.application.package_environment import PackageEnvironmentBuilder
from nervos_core.application.package_health import QualifiedPackageHealthChecker
from nervos_core.application.package_installation import PackageApplicationService
from nervos_core.application.package_query import PackageQueryService
from nervos_core.application.package_signing import Ed25519PackageSigner
from nervos_core.application.package_storage import PackageStore
from nervos_core.application.tool_invocation_mediator import ToolInvocationMediator
from nervos_core.application.tool_registry import ToolDefinitionMaterial, ToolResult
from nervos_core.application.workflows import (
    WorkflowContinuationService,
    WorkflowCreationRequest,
    WorkflowService,
)
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.package_installation import PackageInstallAuthorization
from nervos_core.domain.tools import (
    DefinitionStatus,
    RiskHints,
    ToolSourceKind,
    ToolSourceRef,
    definition_fingerprint,
)
from nervos_core.domain.workflows import WorkflowBudget
from nervos_core.infrastructure.database.packages import (
    SqlAlchemyPackageRegistryPersistence,
    SqlInstalledPackageDefinitionSource,
)
from nervos_core.infrastructure.database.runtime_integration import (
    SqlAlchemyRuntimeIntegrationPersistence,
)
from nervos_core.infrastructure.database.tool_definitions import SqlAlchemyToolDefinitionPersistence
from nervos_core.infrastructure.database.tool_invocations import SqlAlchemyToolInvocationPersistence
from nervos_core.infrastructure.database.tools import (
    SqlAlchemyToolPermissionEvaluator,
    SqlAlchemyToolPermissionPersistence,
)
from nervos_core.infrastructure.database.workflows import (
    SqlAlchemyWorkflowContinuationPersistence,
    SqlAlchemyWorkflowPersistence,
    SqlAlchemyWorkflowStepSource,
)
from nervos_core.infrastructure.sandbox import create_containment
from nervos_mcp.connectors.gmail_registry import register_gmail_reads
from nervos_worker.package_execution import PackageExecutionAdapter
from sqlalchemy import text
from support import RecordingCompletion, build_worker, migrate, run_until_stopped

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "packages/nervos-core/tests/unit"))
from package_fixtures import (  # noqa: E402 # pyright: ignore[reportMissingImports]
    TEST_SIGNING_SEED,
    build_wheel_bytes,
)


@pytest.mark.skipif(
    platform.system() != "Linux" or shutil.which("bwrap") is None,
    reason="actual package qualification requires Linux bubblewrap",
)
@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["research", "mailtriage"])
async def test_research_demo_executes_installed_package_after_worker_restarts(
    tmp_path, monkeypatch, kind
):
    now = datetime.now(UTC)
    engine = migrate(tmp_path / "workflow.db", monkeypatch)
    try:
        store = PackageStore(tmp_path / "packages")
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/prepare_runtime_artifacts.py"),
                "--destination",
                str(store.root / "runtime"),
            ],
            check=True,
            cwd=ROOT,
        )
        registry = SqlAlchemyPackageRegistryPersistence(engine)
        builder = runpy.run_path(str(ROOT / "scripts/build_autonomous_workflow_demos.py"))
        # A genuine MCP catalog identity; only its external search transport is offline.
        schema = {
            "type": "object",
            "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}},
            "additionalProperties": False,
        }
        schema_text = json.dumps(schema, sort_keys=True, separators=(",", ":"))
        manifest = builder["RESEARCH_MANIFEST"].replace(
            b"b70e245d14b66efddee530027ba46a0229da51c09a8ff053deaffb6a1ce91ac1",
            hashlib.sha256(schema_text.encode()).hexdigest().encode(),
        )
        definitions = SqlAlchemyToolDefinitionPersistence(engine)
        mail_descriptors = ()
        if kind == "mailtriage":
            manifest = builder["MAIL_MANIFEST"]
            register_gmail_reads(definitions, lambda: now)
            mail_descriptors = tuple(
                p.to_descriptor()
                for p in definitions.list_for_source(source_ref=BUILTIN_SOURCE_REF)
            )
        read_id = next(
            (d.tool_definition_id for d in mail_descriptors if d.upstream_name.endswith(".get")),
            None,
        )
        archive = package_build_bytes(
            PackageBuildInputs(
                manifest_bytes=manifest,
                config_schema_bytes=builder[
                    "RESEARCH_SCHEMA" if kind == "research" else "MAIL_SCHEMA"
                ],
                agent_wheel_bytes=build_wheel_bytes(
                    name=f"nervos_{kind}",
                    metadata_name=f"nervos-{kind}",
                    members={
                        f"nervos_{kind}/agent.py": builder[
                            "RESEARCH_AGENT" if kind == "research" else "MAIL_AGENT"
                        ]
                    },
                ),
            ),
            signer=Ed25519PackageSigner.from_private_bytes(TEST_SIGNING_SEED),
        )
        staged = tmp_path / "research.nervos"
        staged.write_bytes(archive)
        inspection = PackageQueryService(registry, store).inspect_artifact(staged)
        service = PackageApplicationService(
            registry=registry,
            store=store,
            environment_builder=PackageEnvironmentBuilder(store.environments_root),
            health_checker=QualifiedPackageHealthChecker(containment_factory=create_containment),
            clock=lambda: now,
        )
        service.install(
            staged,
            PackageInstallAuthorization(
                package_id=inspection.package_id,
                package_version=inspection.package_version,
                content_digest=inspection.content_digest,
                signer_fingerprint=inspection.signer_fingerprint,
                archive_digest=inspection.archive_digest,
                approved_by_user_id=1,
                approved_at=now,
            ),
        )
        instance, _ = service.create_package_instance(
            owner_user_id=1,
            package_id=f"com.nervos.{kind}",
            package_version="1.0.0",
            display_name="Research",
            model_provider="anthropic",
            model_name="fixture",
            config={} if kind == "research" else {"read_tool_definition_id": read_id},
        )
        from nervos_core.application.mcp_connections import ConnectionTransport
        from nervos_core.infrastructure.database.mcp_connections import (
            SqlAlchemyMcpConnectionPersistence,
        )

        connections = SqlAlchemyMcpConnectionPersistence(engine)
        source_id = connections.create(
            owner_user_id=1,
            display_name="Search",
            transport=ConnectionTransport.STDIO,
            endpoint=None,
            server_key="fixture",
            credential_ref=None,
            now=now,
        )
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE mcp_connections SET catalog_status='connected' WHERE id=:id"),
                {"id": source_id},
            )
        definitions = SqlAlchemyToolDefinitionPersistence(engine)
        risk = RiskHints(read_only=True, destructive=False)
        fingerprint = definition_fingerprint(
            model_name="nervos__mcp__search",
            upstream_name="search",
            description="Evidence",
            input_schema=schema,
            output_schema=None,
            source_kind=ToolSourceKind.MCP,
            source_id=source_id,
            risk_hints=risk,
        )
        material = ToolDefinitionMaterial(
            ToolSourceKind.MCP,
            source_id,
            "search",
            "nervos__mcp__search",
            "Search",
            "Evidence",
            schema_text,
            None,
            fingerprint,
            DefinitionStatus.AVAILABLE,
            risk,
        )
        definition_id = definitions.insert(material, now=now)
        persisted = definitions.get(definition_id)
        assert persisted is not None
        descriptor = persisted.to_descriptor()
        integration = SqlAlchemyRuntimeIntegrationPersistence(engine)
        if kind == "research":
            integration.bind_tool(1, instance.id, "research.search", definition_id, now)
        SqlAlchemyToolPermissionPersistence(engine).grant_tool(
            owner_user_id=1,
            agent_instance_id=instance.id,
            tool_definition_id=definition_id,
            now=now,
        )
        tools = create_builtin_tool_registry(definitions, clock=lambda: now)

        class Search:
            async def list_tools(self):
                return (descriptor,)

            async def execute(self, descriptor, arguments):
                return ToolResult(
                    "SQLite stores local data. Source: https://sqlite.org/about.html",
                    {"url": "https://sqlite.org/about.html"},
                )

        tools.register(
            source_ref=ToolSourceRef(ToolSourceKind.MCP, source_id),
            source=Search(),
            executor=Search(),
        )
        if kind == "mailtriage":

            class Mailbox:
                async def list_tools(self):
                    return mail_descriptors

                async def execute(self, descriptor, arguments):
                    if descriptor.upstream_name.endswith(".list"):
                        return ToolResult(
                            "Mailbox", {"messages": [{"id": "m1", "thread_id": "t1"}]}
                        )
                    assert arguments == {"message_id": "m1"}
                    return ToolResult(
                        "Invoice", {"id": "m1", "subject": "Invoice", "body_text": "Payment due"}
                    )

            tools.unregister(BUILTIN_SOURCE_REF)
            tools.register(source_ref=BUILTIN_SOURCE_REF, source=Mailbox(), executor=Mailbox())
            for mail_descriptor in mail_descriptors:
                alias = (
                    "gmail.message"
                    if mail_descriptor.upstream_name.endswith(".get")
                    else "gmail.messages"
                )
                integration.bind_tool(
                    1, instance.id, alias, mail_descriptor.tool_definition_id, now
                )
                SqlAlchemyToolPermissionPersistence(engine).grant_tool(
                    owner_user_id=1,
                    agent_instance_id=instance.id,
                    tool_definition_id=mail_descriptor.tool_definition_id,
                    now=now,
                )
        mediator = ToolInvocationMediator(
            registry=tools,
            authorize=SqlAlchemyToolPermissionEvaluator(engine),
            invocations=SqlAlchemyToolInvocationPersistence(engine),
            clock=lambda: datetime.now(UTC),
        )

        adapter = PackageExecutionAdapter(
            store.root,
            mediator=mediator,
            registry=tools,
            packages=registry,
            integrations=integration,
            containment_factory=create_containment,
            workflow_steps=SqlAlchemyWorkflowStepSource(engine),
        )
        resolver = create_composite_agent_definition_resolver(
            [SqlInstalledPackageDefinitionSource(engine)]
        )
        definition = resolver.resolve(AgentDefinitionId(f"com.nervos.{kind}", "1.0.0"))
        workflows = WorkflowService(
            SqlAlchemyWorkflowPersistence(engine), clock=lambda: datetime.now(UTC)
        )
        workflow = workflows.create(
            WorkflowCreationRequest(
                owner_user_id=1,
                agent_instance_id=instance.id,
                workflow_kind="research",
                submission_key="package-research",
                input_text="Local databases",
                definition_id=definition.identity,
                limits=definition.limits,
                budget=WorkflowBudget(),
                now=now,
            )
        )
        completion = RecordingCompletion("anthropic", "Brief with retained source provenance.")
        total_steps = 4 if kind == "research" else 2
        for step in range(total_steps):
            await run_until_stopped(
                build_worker(
                    engine,
                    {"anthropic": completion},
                    worker_id=f"workflow-worker-{step}",
                    clock=lambda: datetime.now(UTC),
                    package_execution=adapter,
                ),
                engine,
            )
            detail = workflows.detail(1, workflow.id)
            with engine.connect() as connection:
                failure = connection.execute(
                    text("SELECT error_code,error_message FROM runs WHERE id=:r"),
                    {"r": detail.steps[-1].run_id},
                ).one()
            assert detail.steps[-1].run_status == "succeeded", failure
            if kind == "mailtriage" and step == 0:
                assert detail.workflow.status.value == "waiting"
                decision = detail.decisions[0]
                assert decision.tool_definition_id == read_id
                workflows.decide(
                    1,
                    workflow.id,
                    decision.id,
                    approve=True,
                    expected_revision=decision.checkpoint_revision,
                )
            if step < total_steps - 1:
                tick = WorkflowContinuationService(
                    SqlAlchemyWorkflowContinuationPersistence(
                        engine, lambda definition_id: resolver.resolve(definition_id).limits
                    ),
                    clock=lambda: datetime.now(UTC),
                ).tick()
                assert tick.dispatched == 1
        assert workflows.get(1, workflow.id).status.value == "succeeded"
        assert completion.calls == (1 if kind == "research" else 0)
        final = workflows.detail(1, workflow.id).checkpoints[-1].state
        if kind == "research":
            assert final["brief"] == "Brief with retained source provenance."
            assert "sqlite.org" in str(final["sources"])
        else:
            assert cast(Any, final["classified"])[0]["category"] == "billing"
    finally:
        engine.dispose()
