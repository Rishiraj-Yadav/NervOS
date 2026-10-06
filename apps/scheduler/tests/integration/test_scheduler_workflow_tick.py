"""W2b — the Scheduler process actually runs the bounded workflow tick.

These are the process-level proofs, not another restatement of the persistence behaviour
already covered in `test_durable_workflows.py`: that the composition *builds* the tick, that
one tick dispatches a due continuation, that a waiting workflow is left alone, and that the
Scheduler's schema gate refuses before any workflow write happens.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from nervos_core.application.workflows import WorkflowCreationRequest, WorkflowService
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.runs import ModelUsage, RunLimits
from nervos_core.domain.workflows import (
    WorkflowBudget,
    WorkflowStatus,
    WorkflowStepResult,
    next_directive,
    waiting_directive,
)
from nervos_core.infrastructure.database import create_sqlite_engine
from nervos_core.infrastructure.database.jobs import SqlAlchemyJobExecutionPersistence
from nervos_core.infrastructure.database.schema_revision import SchemaRevisionMismatch
from nervos_core.infrastructure.database.workflows import (
    SqlAlchemyWorkflowPersistence,
    SqlAlchemyWorkflowStepFinalizer,
)
from nervos_scheduler.app import close_scheduler, create_scheduler
from nervos_scheduler.config import SchedulerSettings
from nervos_scheduler.main import run_scheduler
from sqlalchemy import Engine, text

ROOT = Path(__file__).resolve().parents[4]
# The Scheduler process reads the real clock, so the seeded obligations have to sit in
# real "now" or the deadline check correctly expires them. Five minutes back keeps every
# assertion monotonic without making a "wakes in one second" wait race the Scheduler.
NOW = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)
LIMITS = RunLimits()


def migrate(path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    """One disposable migrated database holding a user and one enabled Chat Instance."""
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
    return engine


def _service(engine: Engine) -> WorkflowService:
    return WorkflowService(SqlAlchemyWorkflowPersistence(engine), clock=lambda: NOW)


def _create(engine: Engine, key: str = "sched") -> int:
    return (
        _service(engine)
        .create(
            WorkflowCreationRequest(
                owner_user_id=1,
                agent_instance_id=1,
                workflow_kind="research",
                submission_key=key,
                input_text="Start the workflow.",
                limits=LIMITS,
                definition_id=AgentDefinitionId("nervos.chat", "1"),
                budget=WorkflowBudget(),
                now=NOW,
            )
        )
        .id
    )


def _run_one_step(
    engine: Engine,
    workflow_id: int,
    result: WorkflowStepResult,
    *,
    now: datetime = NOW,
) -> None:
    """Drive one step to a fenced success, exactly as a Worker would."""
    # The Worker fences the step commit inside the same transaction as the Run's own
    # success. A persistence without the finalizer would silently refuse, which is exactly
    # the property these tests depend on.
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
            now=now,
            workflow_step=result,
        )
        is True
    )
    del workflow_id


def _settings(path: Path, monkeypatch: pytest.MonkeyPatch) -> SchedulerSettings:
    monkeypatch.setenv("NERVOS_DATABASE_PATH", str(path))
    monkeypatch.setenv("NERVOS_ENVIRONMENT", "test")
    return SchedulerSettings()


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(connection.scalar(text(f"SELECT count(*) FROM {table}")))


def test_the_composition_builds_a_workflow_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "compose.db"
    engine = migrate(path, monkeypatch)
    engine.dispose()
    composition = create_scheduler(_settings(path, monkeypatch))
    try:
        # The tick belongs to the ordinary Scheduler process, not to a second daemon that
        # would own the same durable obligation with its own crash recovery.
        assert composition.workflows is not None
        report = composition.workflows.tick()
        assert report.dispatched == 0
        assert report.failed == 0
        assert report.needs_review == 0
    finally:
        close_scheduler(composition)


def test_one_scheduler_tick_dispatches_a_due_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "tick.db"
    engine = migrate(path, monkeypatch)
    workflow_id = _create(engine)
    _run_one_step(
        engine, workflow_id, WorkflowStepResult(directive=next_directive(), state={"r": 1})
    )
    assert _service(engine).get(1, workflow_id).status is WorkflowStatus.RUNNABLE
    engine.dispose()

    composition = create_scheduler(_settings(path, monkeypatch))
    try:
        report = composition.workflows.tick()
        assert report.dispatched == 1
        assert report.examined >= 1
    finally:
        close_scheduler(composition)

    # A brand-new engine models a Scheduler restart: the continuation is already durable.
    reopened = create_sqlite_engine(path)
    try:
        workflow = _service(reopened).get(1, workflow_id)
        assert workflow.status is WorkflowStatus.RUNNING
        assert workflow.step_count == 2
        assert _count(reopened, "runs") == 2
    finally:
        reopened.dispose()


def test_a_waiting_workflow_is_never_dispatched_by_the_scheduler_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "waiting.db"
    engine = migrate(path, monkeypatch)
    workflow_id = _create(engine, key="waiting")
    _run_one_step(
        engine,
        workflow_id,
        WorkflowStepResult(directive=waiting_directive(NOW, seconds=3600), state={}),
    )
    engine.dispose()

    composition = create_scheduler(_settings(path, monkeypatch))
    try:
        # Repeated ticks must not invent the wakeup the step asked for.
        for _ in range(3):
            assert composition.workflows.tick().dispatched == 0
    finally:
        close_scheduler(composition)

    reopened = create_sqlite_engine(path)
    try:
        assert _service(reopened).get(1, workflow_id).status is WorkflowStatus.WAITING
        assert _count(reopened, "runs") == 1
    finally:
        reopened.dispose()


def test_a_due_time_wait_wakes_without_any_owner_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "wake.db"
    engine = migrate(path, monkeypatch)
    workflow_id = _create(engine, key="wake")
    _run_one_step(
        engine,
        workflow_id,
        WorkflowStepResult(directive=waiting_directive(NOW, seconds=1), state={}),
    )
    engine.dispose()

    composition = create_scheduler(_settings(path, monkeypatch))
    try:
        assert composition.workflows.tick().dispatched == 1
    finally:
        close_scheduler(composition)

    reopened = create_sqlite_engine(path)
    try:
        assert _service(reopened).get(1, workflow_id).step_count == 2
    finally:
        reopened.dispose()


def test_the_scheduler_schema_gate_refuses_before_any_workflow_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "gate.db"
    engine = migrate(path, monkeypatch)
    workflow_id = _create(engine, key="gated")
    _run_one_step(engine, workflow_id, WorkflowStepResult(directive=next_directive(), state={}))
    runs_before = _count(engine, "runs")
    # Rewind the applied revision so this binary is one migration ahead of the database.
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE alembic_version SET version_num = :r"),
            {"r": "0023_worker_sandbox_capability"},
        )
    engine.dispose()

    with pytest.raises(SchemaRevisionMismatch):
        run_scheduler(_settings(path, monkeypatch), once=True)

    # The refusal happened before the tick, so no continuation was ever submitted.
    reopened = create_sqlite_engine(path)
    try:
        assert _count(reopened, "runs") == runs_before
        assert _service(reopened).get(1, workflow_id).status is WorkflowStatus.RUNNABLE
    finally:
        reopened.dispose()
