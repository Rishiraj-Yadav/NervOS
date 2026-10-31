# Phase A2: a process-kind package runs through the REAL Worker PackageExecutionAdapter
# with real install/mediation/audit and a fake model provider — same seam as python-kind.
# pyright: basic

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from nervos_core.application.agent_definitions import create_composite_agent_definition_resolver
from nervos_core.application.agents import AgentService
from nervos_core.application.builtin_tools import (
    builtin_tool_specs,
    create_builtin_tool_registry,
    reconcile_builtin_definitions,
)
from nervos_core.application.model_completion import (
    ModelRequest,
    ModelResponse,
    StopOutcome,
)
from nervos_core.application.package_builder import PackageBuildInputs, package_build_bytes
from nervos_core.application.package_environment import PackageEnvironmentBuilder
from nervos_core.application.package_health import SubprocessPackageHealthChecker
from nervos_core.application.package_installation import PackageApplicationService
from nervos_core.application.package_query import PackageQueryService
from nervos_core.application.package_signing import Ed25519PackageSigner
from nervos_core.application.package_storage import PackageStore
from nervos_core.application.sandbox import (
    ContainmentResult,
    ContainmentTier,
    PackageContainment,
    SandboxLaunch,
)
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

CHILD_AGENT = """
import json
import os
import sys
import urllib.request

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(url, body, token):
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json", "authorization": "Bearer " + token},
    )
    with opener.open(request, timeout=60) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def main():
    sys.stdin.readline()
    key = os.environ["OPENAI_API_KEY"]
    status, completion = post(
        os.environ["OPENAI_BASE_URL"] + "/chat/completions",
        {"model": "offline-test-model", "messages": [{"role": "user", "content": "hi"}]},
        key,
    )
    assert status == 200, status
    print(completion["choices"][0]["message"]["content"])
    status, listing = post(
        os.environ["NERVOS_MCP_URL"],
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        key,
    )
    assert status == 200, status
    names = [tool["name"] for tool in listing["result"]["tools"]]
    assert "calculate" in names, names
    status, call = post(
        os.environ["NERVOS_MCP_URL"],
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "calculate", "arguments": {"expression": "6*7"}},
        },
        key,
    )
    assert status == 200, status
    assert call["result"]["isError"] is False, call
    print("TOOL:" + call["result"]["content"][0]["text"])
    print("PROCESS_PACKAGE_OK")


main()
"""

WINDOWS_LAUNCHER = """@echo off
"%NERVOS_PACKAGE_PYTHON%" "%~dp0agent.py" %*
"""

POSIX_LAUNCHER = '#!/bin/sh\nexec "$NERVOS_PACKAGE_PYTHON" "$(dirname "$0")/agent.py" "$@"\n'


class _NoOpContainment(PackageContainment):
    """A recording containment: no sandbox on this machine, launch directly."""

    def __init__(self) -> None:
        self.launches: list[SandboxLaunch] = []

    def prepare(
        self,
        *,
        python: Path,
        scratch: Path,
        environment: Any,
        arguments: Any,
        package_root: Path | None = None,
    ) -> SandboxLaunch:
        # Honor the Worker's scrubbed environment exactly as a real adapter would
        # hand it to the launcher: the child must not see developer credentials.
        launch = SandboxLaunch(
            command=(str(python), *arguments),
            cwd=scratch,
            environment=dict(cast("dict[str, str]", environment)),
            preexec_fn=None,
            start_new_session=False,
        )
        self.launches.append(launch)
        return launch

    def establish(self, process_id: int) -> ContainmentResult:
        del process_id
        return ContainmentResult(ContainmentTier.RESOURCE, "fixture", "no-op double")

    def verify(self, process_id: int) -> None:
        del process_id

    def terminate(self, process_id: int) -> None:
        del process_id

    def release(self, process_id: int) -> None:
        del process_id


class _FakeCompletion:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            text="MEDIATED_MODEL_OK",
            model_provider="fake",
            model_name=request.model_name,
            finish_reason=StopOutcome.STOP,
        )


async def _process_kind_journey(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = migrate(tmp_path / "a2-process.db", monkeypatch)
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

        if sys.platform == "win32":
            launcher_name, launcher_bytes = "assets/launch.cmd", WINDOWS_LAUNCHER
            command = ["assets/launch.cmd"]
        else:
            launcher_name, launcher_bytes = "assets/launch", POSIX_LAUNCHER
            command = ["assets/launch"]
        manifest = (
            b'manifest_version: "1"\n'
            b"package_id: com.acme.process\n"
            b"package_name: acme-process\n"
            b"package_version: 1.0.0\n"
            b"publisher: NervOS test fixture\n"
            b"display_name: Process Acceptance Agent\n"
            b"runtime:\n"
            b"  kind: process\n"
            b"  command:\n"
            + b"".join(f"    - {item}\n".encode() for item in command)
            + b"nervos:\n  min_version: 0.1.0\n  max_version: 0.1.0\n"
            b"configuration:\n  schema: config.schema.json\n"
            + f"assets:\n  - assets/agent.py\n  - {launcher_name}\n".encode()
            + b"tools:\n  required:\n    - calculate\n"
            b"resources:\n  limits:\n    max_model_calls: 2\n"
            b"    max_tool_calls: 4\n    tool_timeout_ms: 10000\n"
        )
        (tmp_path / "stage-agent.py").write_text(CHILD_AGENT, encoding="utf-8")
        (tmp_path / "stage-launcher").write_bytes(launcher_bytes.encode("utf-8"))
        agent_wheel = build_wheel_bytes(members={"acme_process/__init__.py": ""})
        signer = Ed25519PackageSigner.from_private_bytes(TEST_SIGNING_SEED)
        archive = package_build_bytes(
            PackageBuildInputs(
                manifest_bytes=manifest,
                config_schema_bytes=VALID_CONFIG_SCHEMA,
                agent_wheel_bytes=agent_wheel,
                assets={
                    "assets/agent.py": (tmp_path / "stage-agent.py").read_bytes(),
                    launcher_name: (tmp_path / "stage-launcher").read_bytes(),
                },
            ),
            signer=signer,
        )
        staged_file = tmp_path / "process-acceptance.nervos"
        staged_file.write_bytes(archive)

        package_registry = SqlAlchemyPackageRegistryPersistence(engine)
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
        assert installed.package.status.value == "active", installed.package.status

        instance, _binding = package_service.create_package_instance(
            owner_user_id=1,
            package_id="com.acme.process",
            package_version="1.0.0",
            display_name="Process Agent",
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
        mediator = ToolInvocationMediator(
            registry=tool_registry,
            authorize=SqlAlchemyToolPermissionEvaluator(engine),
            invocations=SqlAlchemyToolInvocationPersistence(engine),
            clock=lambda: NOW,
        )
        containment = _NoOpContainment()
        integrations = SqlAlchemyRuntimeIntegrationPersistence(engine)
        adapter = PackageExecutionAdapter(
            package_store.root,
            mediator=mediator,
            registry=tool_registry,
            packages=package_registry,
            integrations=integrations,
            containment_factory=lambda: containment,
            tool_permissions=SqlAlchemyToolPermissionPersistence(engine),
        )

        resolver = create_composite_agent_definition_resolver(
            [SqlInstalledPackageDefinitionSource(engine)]
        )
        agent_service = AgentService(
            SqlAlchemyAgentPersistence(create_session_factory(engine)),
            resolver,
            lambda: NOW,
            compose_model_providers(None, None).catalog,
            SqlAlchemyJobPersistence(engine),
        )
        run = agent_service.submit_run(1, instance.id, "run the process agent")

        completion = _FakeCompletion()
        worker = build_worker(engine, {"gemini": completion}, package_execution=adapter)
        await run_until_stopped(worker, engine)

        result = agent_service.get_run(1, run.id)
        assert result.status is RunStatus.SUCCEEDED, (
            result.error_code,
            result.error_message,
        )
        assert "MEDIATED_MODEL_OK" in (result.output_text or ""), result.output_text
        assert "TOOL:" in (result.output_text or ""), result.output_text
        assert "PROCESS_PACKAGE_OK" in (result.output_text or ""), result.output_text
        assert len(completion.requests) == 1
        assert completion.requests[0].model_name == "offline-test-model"
        with engine.connect() as connection:
            invocation = connection.execute(
                text("SELECT upstream_name, status FROM tool_invocations WHERE run_id=:run_id"),
                {"run_id": run.id},
            ).one()
        assert invocation == ("calculate", "succeeded"), invocation
    finally:
        engine.dispose()


# Phase A2 (un-skipped): ADR 0046 + migration 0025 made process-kind packages
# installable through the real path; the entrypoint columns stay NULL for process
# rows and the run-executable snapshot carries the stable `process:<command[0]>`
# label in the existing package_entrypoint column.
@pytest.mark.anyio
async def test_process_kind_package_through_real_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _process_kind_journey(tmp_path, monkeypatch)
