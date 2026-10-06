"""ADR 0037/0038: unqualified platforms refuse package execution before any spawn.

The production launcher is resolved before package code exists. On a platform without a
qualified filesystem/network isolation backend, the adapter maps that refusal to the static
`sandbox_unavailable` outcome without starting a child process at all. Trusted built-in
agents never use this adapter, so their existing execution path is unaffected.
"""

# pyright: basic

from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from nervos_core.application.model_completion import ModelProviderError
from nervos_core.domain.execution import RunExecutableSnapshot, RunExecutionKind
from nervos_core.domain.runs import Run, RunLimits, RunStatus
from nervos_package_host.wire import HOST_PROTOCOL_VERSION
from nervos_worker import package_execution as package_execution_module
from nervos_worker.package_execution import PackageExecutionAdapter
from support import NOW, RecordingCompletion

LINUX_BWRAP = platform.system() == "Linux" and shutil.which("bwrap") is not None


def _package_run(environment_digest: str) -> Run:
    return Run(
        id=1,
        agent_instance_id=1,
        agent_key="com.acme.untrusted",
        agent_definition_version="1.0.0",
        model_provider="gemini",
        model_name="offline-fixture",
        input_text="this input must never reach package code",
        limits=RunLimits(),
        status=RunStatus.RUNNING,
        created_at=NOW,
        started_at=NOW,
        executable=RunExecutableSnapshot(
            execution_kind=RunExecutionKind.PACKAGE,
            installed_package_version_id=7,
            package_content_digest="a" * 64,
            package_environment_id=9,
            package_environment_digest=environment_digest,
            package_entrypoint="untrusted.agent:Agent",
            effective_config_json="{}",
            effective_config_digest=None,
            agent_instance_config_revision=1,
            host_protocol_version=HOST_PROTOCOL_VERSION,
            sdk_api_version="0.1",
        ),
    )


@pytest.mark.skipif(
    LINUX_BWRAP,
    reason="this test covers unqualified platforms; Linux with bwrap is qualified separately",
)
@pytest.mark.anyio
async def test_package_execution_refuses_before_any_spawn_on_unqualified_platforms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = "d" * 64
    store = tmp_path / "package-store"
    python = (
        store
        / "environments"
        / digest
        / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    python.parent.mkdir(parents=True)
    python.write_bytes(b"would-have-been-executed")

    def forbidden_spawn(*args: object, **kwargs: object) -> None:
        pytest.fail("an unqualified platform must refuse before any package process exists")

    monkeypatch.setattr(package_execution_module, "_spawn_host", forbidden_spawn)
    adapter = PackageExecutionAdapter(store)
    with pytest.raises(ModelProviderError) as refusal:
        await adapter.run(
            RecordingCompletion(),
            _package_run(digest),
            SimpleNamespace(attempt_id=1),  # type: ignore[arg-type]
            0,
        )
    assert refusal.value.code == "sandbox_unavailable"
