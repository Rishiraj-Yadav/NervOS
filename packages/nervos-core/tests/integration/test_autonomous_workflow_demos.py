"""W6 — the two demo workflows, on one runtime, end to end.

These tests are the milestone's acceptance evidence for the application half of W6. They
drive the real persistence, the real fenced commit, and the real Scheduler tick; only the
model provider and the MCP provider are replaced, because a demo that called a live model
would prove nothing about durability.

The properties that matter:

* Both demos run on the **same** runtime. A workflow foundation that only works for one
  domain is not a foundation.
* A workflow survives a **restart mid-workflow** and continues from its checkpoint.
* The mail demo **never sends**. It stops at an owner decision, which is the whole point of
  shipping reading before writing.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from nervos_core.application.agent_definitions import create_composite_agent_definition_resolver
from nervos_core.application.package_archive import ArchiveValidationProfile, BoundedArchiveReader
from nervos_core.application.package_builder import MANIFEST_PATH
from nervos_core.application.package_manifest import parse_package_manifest
from nervos_core.application.workflows import (
    WorkflowContinuationService,
    WorkflowCreationRequest,
    WorkflowService,
    step_input_text,
)
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.packages import PackageManifest
from nervos_core.domain.runs import ModelUsage, RunLimits
from nervos_core.domain.workflows import (
    FrozenJSONValue,
    WorkflowBudget,
    WorkflowDecisionRequest,
    WorkflowDirective,
    WorkflowDirectiveKind,
    WorkflowStatus,
    WorkflowStepResult,
    WorkflowWaitKind,
    decision_arguments_digest,
    freeze_state,
    next_directive,
)
from nervos_core.infrastructure.database import create_sqlite_engine
from nervos_core.infrastructure.database.jobs import SqlAlchemyJobExecutionPersistence
from nervos_core.infrastructure.database.packages import SqlInstalledPackageDefinitionSource
from nervos_core.infrastructure.database.workflows import (
    SqlAlchemyWorkflowContinuationPersistence,
    SqlAlchemyWorkflowPersistence,
    SqlAlchemyWorkflowStepFinalizer,
)
from sqlalchemy import Engine, text

ROOT = Path(__file__).resolve().parents[4]
# The Scheduler tick reads a real clock, so the seeded obligations sit in real "now".
NOW = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)
LIMITS = RunLimits()
RESEARCH = ROOT / "artifacts" / "demos" / "research-workflow.nervos"
MAIL = ROOT / "artifacts" / "demos" / "mail-triage.nervos"


# ---------------------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------------------


def _database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Engine, Path]:
    """One disposable migrated database with an owner and an enabled Chat Instance."""
    path = tmp_path / "demos.db"
    monkeypatch.setenv("NERVOS_ENVIRONMENT", "test")
    monkeypatch.setenv("NERVOS_DATABASE_PATH", str(path))
    command.upgrade(Config(str(ROOT / "apps" / "api" / "alembic.ini")), "head")
    engine = create_sqlite_engine(path)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users"
                "(username,password_hash,role,is_active,created_at,updated_at)"
                " VALUES('owner',X'00','admin',1,:n,:n)"
            ),
            {"n": NOW},
        )
        connection.execute(
            text(
                "INSERT INTO agent_instances(owner_user_id,agent_key,agent_definition_version,"
                "display_name,enabled,model_provider,model_name,created_at,updated_at)"
                " VALUES(1,'nervos.chat','1','Agent',1,'anthropic','opaque/model',:n,:n)"
            ),
            {"n": NOW},
        )
        # A decision binds to a real reviewed tool identity, so the demo needs a real one.
        # A built-in source needs no MCP connection row, keeping this fixture free of any
        # account or credential setup.
        connection.execute(
            text(
                "INSERT INTO tool_definitions"
                "(source_kind,source_id,upstream_name,model_name,display_name,description,"
                "input_schema,output_schema,fingerprint,status,created_at,updated_at)"
                " VALUES('builtin',NULL,'gmail.users.messages.send','gmail_messages_send',"
                "'Send draft','proposed only','{}',NULL,:f,'available',:n,:n)"
            ),
            {"f": "c" * 64, "n": NOW},
        )
    return engine, path


def _control(engine: Engine) -> WorkflowService:
    return WorkflowService(SqlAlchemyWorkflowPersistence(engine), clock=lambda: NOW)


def _tick(engine: Engine) -> WorkflowContinuationService:
    resolver = create_composite_agent_definition_resolver(
        [SqlInstalledPackageDefinitionSource(engine)]
    )

    def limits_for(definition_id: AgentDefinitionId) -> RunLimits:
        return resolver.resolve(definition_id).limits

    return WorkflowContinuationService(
        SqlAlchemyWorkflowContinuationPersistence(engine, limits_for), clock=lambda: NOW
    )


def _create(engine: Engine, key: str, request: str, kind: str) -> int:
    return (
        _control(engine)
        .create(
            WorkflowCreationRequest(
                owner_user_id=1,
                agent_instance_id=1,
                workflow_kind=kind,
                submission_key=key,
                input_text=request,
                limits=LIMITS,
                definition_id=AgentDefinitionId("nervos.chat", "1"),
                budget=WorkflowBudget(),
                now=NOW,
            )
        )
        .id
    )


def _finish_step(engine: Engine, result: WorkflowStepResult) -> None:
    """Drive one dispatched step to a fenced success, exactly as a Worker would."""
    persistence = SqlAlchemyJobExecutionPersistence(
        engine, workflow_steps=SqlAlchemyWorkflowStepFinalizer()
    )
    claim = persistence.claim_next(
        worker_id="worker-1",
        provider_ids=("anthropic",),
        max_active=4,
        now=NOW,
        lease_duration=timedelta(seconds=60),
    )
    assert claim is not None
    assert persistence.start_attempt(claim, now=NOW) is True
    assert (
        persistence.succeed(
            claim,
            output_text="step done",
            finish_reason="stop",
            usage=ModelUsage(input_tokens=5, output_tokens=3, total_tokens=8),
            elapsed_ms=3,
            now=NOW,
            workflow_step=result,
        )
        is True
    )


def _decision_wait(
    state: Mapping[str, FrozenJSONValue],
) -> WorkflowStepResult:
    """A step that stops and asks the owner, rather than acting on its own."""
    arguments = freeze_state({"thread_id": "m1"})
    return WorkflowStepResult(
        directive=WorkflowDirective(
            kind=WorkflowDirectiveKind.WAIT,
            wait_kind=WorkflowWaitKind.OWNER_DECISION,
            decision_key="k",
        ),
        state=state,
        decision_request=WorkflowDecisionRequest(
            checkpoint_revision=0,
            tool_definition_id=1,
            upstream_name="gmail.users.messages.send",
            action_fingerprint="a" * 64,
            arguments=arguments,
            arguments_digest=decision_arguments_digest(arguments),
            preview=freeze_state({"subject": "hello"}),
            package_content_digest=None,
            effective_config_digest=None,
            expires_at=NOW + timedelta(hours=1),
        ),
    )


# ---------------------------------------------------------------------------------------
# Fixtures — the demo artifacts are built, never committed
# ---------------------------------------------------------------------------------------

# Referenced so static analysis does not mistake the autouse fixture for dead code.
__all__ = ["_built_demo_artifacts"]


@pytest.fixture(scope="session", autouse=True)
def _built_demo_artifacts() -> None:
    """Build the demo archives when absent, exactly as the W6 runbook instructs.

    The demo `.nervos` artifacts are deterministic build outputs and are deliberately
    not committed, so a fresh checkout (CI) must build them before the artifact tests
    run. Building is offline and deterministic; when the artifacts are already present
    the builder refreshes them in place at no cost to the properties under test.
    """
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "build_autonomous_workflow_demos.py"),
            str(ROOT / "artifacts" / "demos"),
        ],
        cwd=ROOT,
        check=True,
    )


# ---------------------------------------------------------------------------------------
# The artifacts exist and are real packages
# ---------------------------------------------------------------------------------------


def _manifest_of(artifact: Path) -> PackageManifest:
    """Parse a demo's manifest through the real reader, exactly as an install would."""
    reader = BoundedArchiveReader(artifact, profile=ArchiveValidationProfile.NERVOS_V1)
    return parse_package_manifest(reader.read(MANIFEST_PATH))


def test_both_demos_build_into_verified_packages() -> None:
    for artifact in (RESEARCH, MAIL):
        assert artifact.exists(), (
            f"{artifact.name} is missing; run scripts/build_autonomous_workflow_demos.py"
        )
        manifest = _manifest_of(artifact)
        assert manifest.package_id
        assert manifest.runtime.entrypoint


def test_both_demos_declare_the_workflow_host_feature() -> None:
    # Without this declaration the Worker refuses the host before any package code runs, so
    # a demo that did not declare it could never have executed at all.
    for artifact in (RESEARCH, MAIL):
        assert b"workflow: workflow-v1" in artifact.read_bytes(), artifact.name


def test_the_mail_demo_declares_only_read_only_gmail_tools() -> None:
    raw = MAIL.read_bytes()
    assert b"upstream_name: gmail.users.messages.list" in raw
    assert b"upstream_name: gmail.users.messages.get" in raw
    # The send tool appears only inside the agent's decision proposal, never as a declared
    # dependency it could invoke without asking.
    assert b"upstream_name: gmail.users.messages.send" not in raw


# ---------------------------------------------------------------------------------------
# One runtime, two domains
# ---------------------------------------------------------------------------------------


def test_both_demos_advance_on_the_same_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _path = _database(tmp_path, monkeypatch)
    research_id = _create(engine, "demo-research", "durable research", "research")
    mail_id = _create(engine, "demo-mail", "triage my inbox", "mail_triage")

    # Creation already dispatches step 1, so both are `running`. A tick must not invent a
    # second step for a workflow that has not finished one.
    assert _control(engine).get(1, research_id).status is WorkflowStatus.RUNNING
    assert _control(engine).get(1, mail_id).status is WorkflowStatus.RUNNING
    assert _tick(engine).tick().dispatched == 0

    _finish_step(
        engine,
        WorkflowStepResult(directive=next_directive(), state=freeze_state({"sources": ["s1"]})),
    )
    _finish_step(engine, _decision_wait(freeze_state({"classified": [{"id": "m1"}]})))

    # One tick advances exactly the workflow that asked to advance. The mail workflow asked
    # to wait for an owner decision, and a tick must never invent that decision -- so
    # dispatching one, not two, is the correct outcome, not a shortfall.
    report = _tick(engine).tick()
    assert report.dispatched == 1

    # And they advanced for genuinely different reasons.
    research = _control(engine).get(1, research_id)
    mail = _control(engine).get(1, mail_id)
    assert research.status is WorkflowStatus.RUNNING
    assert mail.status is WorkflowStatus.WAITING
    assert mail.wait_kind is WorkflowWaitKind.OWNER_DECISION


def test_two_demos_do_not_share_checkpoint_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _path = _database(tmp_path, monkeypatch)
    research_id = _create(engine, "isolated-research", "topic B", "research")
    mail_id = _create(engine, "isolated-mail", "inbox", "mail_triage")

    _finish_step(
        engine,
        WorkflowStepResult(directive=next_directive(), state=freeze_state({"sources": ["s1"]})),
    )
    _finish_step(
        engine,
        WorkflowStepResult(directive=next_directive(), state=freeze_state({"classified": []})),
    )

    research_keys = set(_control(engine).detail(1, research_id).checkpoints[-1].state)
    mail_keys = set(_control(engine).detail(1, mail_id).checkpoints[-1].state)
    assert research_keys == {"sources"}
    assert mail_keys == {"classified"}


# ---------------------------------------------------------------------------------------
# Restart mid-workflow
# ---------------------------------------------------------------------------------------


def test_a_research_workflow_resumes_from_its_checkpoint_after_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, path = _database(tmp_path, monkeypatch)
    workflow_id = _create(engine, "restart-research", "topic A", "research")

    _finish_step(
        engine,
        WorkflowStepResult(
            directive=next_directive(), state=freeze_state({"topic": "A", "sources": ["s1", "s2"]})
        ),
    )
    assert _control(engine).get(1, workflow_id).status is WorkflowStatus.RUNNABLE
    engine.dispose()  # A brand-new engine models a process restart.

    reopened = create_sqlite_engine(path)
    try:
        assert _tick(reopened).tick().dispatched == 1
        assert _control(reopened).get(1, workflow_id).status is WorkflowStatus.RUNNING

        detail = _control(reopened).detail(1, workflow_id)
        assert detail.checkpoints, "the completed step must have committed a checkpoint"
        # Step 2 resumes from the revision step 1 committed, so it expects exactly that one.
        assert detail.steps[-1].step.expected_checkpoint_revision == 1
        assert detail.workflow.checkpoint_revision == 1
        assert set(detail.steps[-1].step.state) == {"topic", "sources"}

        # The step's bounded Run input is derived from durable facts and carries no state,
        # so a checkpoint can never be smuggled through a smaller column.
        derived = step_input_text("research", 2, detail.steps[-1].step.expected_checkpoint_revision)
        assert "research" in derived
        assert "sources" not in derived
    finally:
        reopened.dispose()


def test_a_waiting_workflow_survives_a_restart_without_being_advanced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, path = _database(tmp_path, monkeypatch)
    workflow_id = _create(engine, "restart-mail", "inbox", "mail_triage")
    _finish_step(engine, _decision_wait(freeze_state({"classified": [{"id": "m1"}]})))
    engine.dispose()

    reopened = create_sqlite_engine(path)
    try:
        # Repeated ticks must never invent the owner decision the step asked for.
        for _ in range(3):
            assert _tick(reopened).tick().dispatched == 0
        workflow = _control(reopened).get(1, workflow_id)
        assert workflow.status is WorkflowStatus.WAITING
        assert workflow.signal_key is None and workflow.decision_key == "k"
    finally:
        reopened.dispose()
