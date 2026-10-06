"""W1-W3 durable autonomous workflow acceptance tests (ADR 0039).

Every test runs against its own migrated temporary database, uses a deterministic clock, and
proves a public behavior: owner isolation, fenced atomicity, restart persistence,
continuation uniqueness, bounded waits, signals, pause/cancel, and owner decisions.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from execution_support import NOW, migrate
from nervos_core.application.agent_definitions import (
    create_composite_agent_definition_resolver,
)
from nervos_core.application.agents import AgentService
from nervos_core.application.job_execution import ClaimedAttempt
from nervos_core.application.workflows import (
    SIGNAL_ACCEPTED,
    SIGNAL_NOT_WAITING,
    SIGNAL_REPLAYED,
    SIGNAL_STALE_REVISION,
    WorkflowCapacityExceeded,
    WorkflowContinuationService,
    WorkflowCreationRequest,
    WorkflowService,
    reservation_for,
)
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.runs import RunLimits, RunStatus
from nervos_core.domain.workflows import (
    CHECKPOINT_MAX_BYTES,
    WORKFLOW_JOB_MAX_ATTEMPTS,
    InvalidWorkflow,
    WorkflowBudget,
    WorkflowConflict,
    WorkflowDecisionState,
    WorkflowDirectiveKind,
    WorkflowNotFound,
    WorkflowNotLinked,
    WorkflowStatus,
    WorkflowStepResult,
    WorkflowTransitionError,
    WorkflowWaitKind,
    complete_directive,
    decision_directive,
    freeze_state,
    next_directive,
    signal_directive,
    state_digest,
    waiting_directive,
)
from nervos_core.infrastructure.database import create_session_factory
from nervos_core.infrastructure.database.agents import SqlAlchemyAgentPersistence
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobExecutionPersistence,
    SqlAlchemyJobPersistence,
)
from nervos_core.infrastructure.database.packages import SqlInstalledPackageDefinitionSource
from nervos_core.infrastructure.database.workflows import (
    SqlAlchemyWorkflowContinuationPersistence,
    SqlAlchemyWorkflowPersistence,
    SqlAlchemyWorkflowStepFinalizer,
    WorkflowLimitsResolver,
)
from sqlalchemy import Engine, text

LIMITS = RunLimits()


class _Clock:
    def __init__(self, instant: datetime = NOW) -> None:
        self.instant = instant

    def __call__(self) -> datetime:
        return self.instant

    def advance(self, seconds: int) -> datetime:
        self.instant += timedelta(seconds=seconds)
        return self.instant


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    created = migrate(tmp_path / "workflows.db", monkeypatch)
    try:
        yield created
    finally:
        created.dispose()


def _services(
    engine: Engine, clock: _Clock
) -> tuple[WorkflowService, SqlAlchemyWorkflowPersistence]:
    persistence = SqlAlchemyWorkflowPersistence(engine)
    return WorkflowService(persistence, clock=clock), persistence


def _create(
    service: WorkflowService,
    *,
    instance_id: int = 1,
    key: str = "wf-1",
    kind: str = "research",
    now: datetime = NOW,
    budget: WorkflowBudget | None = None,
) -> int:
    workflow = service.create(
        WorkflowCreationRequest(
            owner_user_id=1,
            agent_instance_id=instance_id,
            workflow_kind=kind,
            submission_key=key,
            input_text="Begin the research workflow.",
            limits=LIMITS,
            definition_id=AgentDefinitionId("nervos.chat", "1"),
            budget=budget or WorkflowBudget(),
            now=now,
        )
    )
    return workflow.id


# ---------------------------------------------------------------------------------------
# W1: creation, owner isolation, replay identity
# ---------------------------------------------------------------------------------------


def test_creation_commits_a_running_workflow_with_a_linked_first_step(
    engine: Engine,
) -> None:
    clock = _Clock()
    service, _ = _services(engine, clock)

    workflow_id = _create(service)

    detail = service.detail(1, workflow_id)
    assert detail.workflow.status is WorkflowStatus.RUNNING
    assert detail.workflow.step_count == 1
    assert len(detail.steps) == 1
    step = detail.steps[0]
    assert step.step.step_number == 1
    assert step.step.status.value == "running"
    assert step.run_id is not None
    with engine.connect() as connection:
        run = connection.execute(
            text(
                "SELECT runs.status, jobs.max_attempts FROM runs"
                " JOIN jobs ON jobs.run_id = runs.id WHERE runs.id = :r"
            ),
            {"r": step.run_id},
        ).one()
    # A workflow Job has exactly one Attempt: a failed step may already have caused an
    # external effect, so the ordinary retry ladder must not replay it.
    assert run.status == RunStatus.CREATED.value
    assert run.max_attempts == WORKFLOW_JOB_MAX_ATTEMPTS


def test_an_identical_creation_replays_and_a_changed_body_conflicts(engine: Engine) -> None:
    clock = _Clock()
    service, _ = _services(engine, clock)

    first = service.create(
        WorkflowCreationRequest(
            owner_user_id=1,
            agent_instance_id=1,
            workflow_kind="research",
            submission_key="replay",
            input_text="Start.",
            limits=LIMITS,
            definition_id=AgentDefinitionId("nervos.chat", "1"),
            budget=WorkflowBudget(),
            now=NOW,
        )
    )
    second = service.create(
        WorkflowCreationRequest(
            owner_user_id=1,
            agent_instance_id=1,
            workflow_kind="research",
            submission_key="replay",
            input_text="Start.",
            limits=LIMITS,
            definition_id=AgentDefinitionId("nervos.chat", "1"),
            budget=WorkflowBudget(),
            now=NOW,
        )
    )
    assert second.id == first.id
    with engine.connect() as connection:
        runs = int(connection.scalar(text("SELECT count(*) FROM runs")))
    assert runs == 1

    with pytest.raises(WorkflowConflict):
        service.create(
            WorkflowCreationRequest(
                owner_user_id=1,
                agent_instance_id=1,
                workflow_kind="research",
                submission_key="replay",
                input_text="Different body.",
                limits=LIMITS,
                definition_id=AgentDefinitionId("nervos.chat", "1"),
                budget=WorkflowBudget(),
                now=NOW,
            )
        )


def test_a_foreign_or_missing_workflow_is_indistinguishable(engine: Engine) -> None:
    clock = _Clock()
    service, persistence = _services(engine, clock)
    workflow_id = _create(service)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users(username,password_hash,role,is_active,created_at,updated_at)"
                " VALUES('other',X'00','admin',1,:n,:n)"
            ),
            {"n": NOW},
        )

    # The surface must not be able to probe for another owner's workflow.
    with pytest.raises(WorkflowNotFound):
        service.get(2, workflow_id)
    with pytest.raises(WorkflowNotFound):
        service.get(2, 999_999)
    assert service.get(1, workflow_id).id == workflow_id
    del persistence


def test_an_active_workflow_ceiling_is_enforced_per_owner(engine: Engine) -> None:
    clock = _Clock()
    limited = WorkflowService(SqlAlchemyWorkflowPersistence(engine), clock=clock, max_active=2)
    for index in range(2):
        limited.create(
            WorkflowCreationRequest(
                owner_user_id=1,
                agent_instance_id=1,
                workflow_kind="research",
                submission_key=f"bounded-{index}",
                input_text=f"Start {index}.",
                limits=LIMITS,
                definition_id=AgentDefinitionId("nervos.chat", "1"),
                budget=WorkflowBudget(),
                now=NOW,
            )
        )
    with pytest.raises(WorkflowCapacityExceeded):
        limited.create(
            WorkflowCreationRequest(
                owner_user_id=1,
                agent_instance_id=1,
                workflow_kind="research",
                submission_key="bounded-3",
                input_text="Start 3.",
                limits=LIMITS,
                definition_id=AgentDefinitionId("nervos.chat", "1"),
                budget=WorkflowBudget(),
                now=NOW,
            )
        )


# ---------------------------------------------------------------------------------------
# W1: the fenced checkpoint boundary
# ---------------------------------------------------------------------------------------


def _claim_and_start(engine: Engine, workflow_id: int) -> ClaimedAttempt:
    persistence = SqlAlchemyJobExecutionPersistence(engine)
    with engine.connect() as connection:
        run_id = int(
            connection.scalar(
                text(
                    "SELECT run_id FROM workflow_steps WHERE workflow_id = :w AND status='running'"
                ),
                {"w": workflow_id},
            )
        )
    claim = persistence.claim_next(
        worker_id="worker-1",
        provider_ids=("anthropic",),
        max_active=4,
        now=NOW,
        lease_duration=timedelta(seconds=60),
    )
    assert claim is not None and claim.run_id == run_id
    assert persistence.start_attempt(claim, now=NOW) is True
    return claim


def test_legacy_success_completes_linked_workflow_inside_success_fence(engine: Engine) -> None:
    service, _ = _services(engine, _Clock())
    workflow_id = _create(service)
    claim = _claim_and_start(engine, workflow_id)
    persistence = SqlAlchemyJobExecutionPersistence(
        engine, workflow_steps=SqlAlchemyWorkflowStepFinalizer()
    )
    assert persistence.succeed(
        claim, output_text="Done", finish_reason="stop", usage=_usage(), elapsed_ms=1, now=NOW
    )
    assert service.get(1, workflow_id).status is WorkflowStatus.SUCCEEDED
    assert service.get(1, workflow_id).checkpoint_revision == 1


@pytest.mark.parametrize("terminal", ["failed", "cancelled", "succeeded"])
def test_scheduler_reconciles_terminal_run_without_replaying(engine: Engine, terminal: str) -> None:
    service, _ = _services(engine, _Clock())
    workflow_id = _create(service)
    # Model every terminal path, including a legacy writer without a committer.
    claim = _claim_and_start(engine, workflow_id)
    persistence = SqlAlchemyJobExecutionPersistence(engine)
    if terminal == "succeeded":
        assert persistence.succeed(
            claim, output_text="done", finish_reason="stop", usage=_usage(), elapsed_ms=1, now=NOW
        )
    elif terminal == "failed":
        from nervos_core.domain.jobs import RetryDisposition

        assert persistence.fail(
            claim,
            error_code="model_request_rejected",
            error_message="The model provider rejected the request configuration.",
            retry_disposition=RetryDisposition.DO_NOT_RETRY,
            usage=_usage(),
            elapsed_ms=1,
            now=NOW,
        )
    else:
        from nervos_core.infrastructure.database.jobs import SqlAlchemyRunCancellationPersistence

        SqlAlchemyRunCancellationPersistence(engine).cancel_run(
            user_id=1, run_id=claim.run_id, now=NOW
        )
    report = _tick(engine, _Clock()).tick()
    assert report.needs_review == 1
    assert service.get(1, workflow_id).status is WorkflowStatus.NEEDS_REVIEW
    assert _tick(engine, _Clock()).tick().dispatched == 0
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM runs")) == 1


def test_a_fenced_step_success_commits_the_run_and_its_state_together(
    engine: Engine,
) -> None:
    clock = _Clock()
    service, _ = _services(engine, clock)
    workflow_id = _create(service)
    claim = _claim_and_start(engine, workflow_id)
    persistence = SqlAlchemyJobExecutionPersistence(
        engine, workflow_steps=SqlAlchemyWorkflowStepFinalizer()
    )

    committed = persistence.succeed(
        claim,
        output_text="Collected two sources.",
        finish_reason="stop",
        usage=_usage(),
        elapsed_ms=12,
        now=NOW,
        workflow_step=WorkflowStepResult(
            directive=next_directive(),
            state=freeze_state({"sources": ["a", "b"]}),
            summary="collected",
        ),
    )

    assert committed is True
    detail = service.detail(1, workflow_id)
    assert detail.workflow.checkpoint_revision == 1
    assert detail.workflow.status is WorkflowStatus.RUNNABLE
    assert len(detail.checkpoints) == 1
    # The frozen state an owner inspects is immutable: arrays are tuples.
    assert detail.checkpoints[0].state_digest == state_digest(freeze_state({"sources": ["a", "b"]}))
    assert detail.checkpoints[0].step_number == 1
    assert detail.steps[0].step.status.value == "succeeded"
    assert detail.steps[0].step.summary == "collected"
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT status FROM runs WHERE id=:r"), {"r": claim.run_id})
            == "succeeded"
        )


def test_a_stale_attempt_cannot_advance_the_workflow(engine: Engine) -> None:
    clock = _Clock()
    service, _ = _services(engine, clock)
    workflow_id = _create(service)
    claim = _claim_and_start(engine, workflow_id)
    finalizer = SqlAlchemyWorkflowStepFinalizer()

    # Move the durable revision under the step while the Attempt is still leased.
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE workflow_executions SET checkpoint_revision = 7 WHERE id = :w"),
            {"w": workflow_id},
        )
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        committed = finalizer.commit_step(
            connection,
            run_id=claim.run_id,
            attempt_id=claim.attempt_id,
            result=WorkflowStepResult(directive=next_directive(), state={}),
            now=NOW,
        )
        connection.commit()
    assert committed is False
    with engine.connect() as connection:
        assert (
            int(
                connection.scalar(
                    text("SELECT count(*) FROM workflow_checkpoints WHERE workflow_id = :w"),
                    {"w": workflow_id},
                )
            )
            == 0
        )


def test_a_run_without_a_workflow_step_is_never_treated_as_one(engine: Engine) -> None:
    """An ordinary Run keeps exactly its existing behaviour, including under the finalizer."""
    clock = _Clock()
    agents = SqlAlchemyAgentPersistence(create_session_factory(engine))
    from nervos_core.application.model_providers import ModelProviderCatalog

    agent_service = AgentService(
        agents,
        create_composite_agent_definition_resolver([SqlInstalledPackageDefinitionSource(engine)]),
        clock,
        providers=ModelProviderCatalog((), known=("anthropic",)),
        submissions=SqlAlchemyJobPersistence(engine),
    )
    run = agent_service.submit_run(1, 1, "An ordinary chat Run.")
    finalizer = SqlAlchemyWorkflowStepFinalizer()
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        with pytest.raises(WorkflowNotLinked):
            finalizer.commit_step(
                connection,
                run_id=run.id,
                attempt_id=1,
                result=WorkflowStepResult(directive=next_directive(), state={}),
                now=NOW,
            )
        connection.rollback()
    assert run.executable.execution_kind.value == "builtin"


def test_an_oversized_checkpoint_fails_before_the_run_is_terminalized() -> None:
    """Package data that violates the bounds must not be able to strand a succeeded Run."""
    with pytest.raises(InvalidWorkflow):
        WorkflowStepResult(
            directive=next_directive(), state={"blob": "x" * (CHECKPOINT_MAX_BYTES + 1)}
        )


def _usage():
    from nervos_core.domain.runs import ModelUsage

    return ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15)


def _builtin_resolver(engine: Engine) -> WorkflowLimitsResolver:
    """Resolve one pinned agent definition's ordinary per-step Run limits."""
    resolver = create_composite_agent_definition_resolver(
        [SqlInstalledPackageDefinitionSource(engine)]
    )

    def limits_for(definition_id: AgentDefinitionId) -> RunLimits:
        return resolver.resolve(definition_id).limits

    return limits_for


# ---------------------------------------------------------------------------------------
# W2: continuations, waits, signals, lifecycle
# ---------------------------------------------------------------------------------------


def _tick(engine: Engine, clock: _Clock) -> WorkflowContinuationService:
    """A Scheduler tick wired with the real definition resolver and the frozen limits."""
    persistence = SqlAlchemyWorkflowContinuationPersistence(engine, _builtin_resolver(engine))
    return WorkflowContinuationService(persistence, clock=clock)


def _finish_step(
    engine: Engine, workflow_id: int, result: WorkflowStepResult, *, now: datetime = NOW
) -> None:
    persistence = SqlAlchemyJobExecutionPersistence(
        engine, workflow_steps=SqlAlchemyWorkflowStepFinalizer()
    )
    claim = _claim_and_start(engine, workflow_id)
    assert (
        persistence.succeed(
            claim,
            output_text="step done",
            finish_reason="stop",
            usage=_usage(),
            elapsed_ms=5,
            now=now,
            workflow_step=result,
        )
        is True
    )


def test_a_next_directive_submits_exactly_one_linked_continuation(engine: Engine) -> None:
    clock = _Clock()
    service, _ = _services(engine, clock)
    workflow_id = _create(service)
    _finish_step(
        engine, workflow_id, WorkflowStepResult(directive=next_directive(), state={"n": 1})
    )

    tick = _tick(engine, clock).tick()

    assert tick.dispatched == 1
    detail = service.detail(1, workflow_id)
    assert detail.workflow.status is WorkflowStatus.RUNNING
    assert detail.workflow.step_count == 2
    steps = detail.steps
    assert [s.step.step_number for s in steps] == [1, 2]
    assert steps[1].step.expected_checkpoint_revision == 1
    assert steps[1].step.state_digest == state_digest(freeze_state({"n": 1}))
    assert steps[1].run_id is not None and steps[1].run_id != steps[0].run_id


def test_a_second_tick_never_creates_a_second_continuation(engine: Engine) -> None:
    clock = _Clock()
    service, _ = _services(engine, clock)
    workflow_id = _create(service)
    _finish_step(engine, workflow_id, WorkflowStepResult(directive=next_directive(), state={}))
    service_tick = _tick(engine, clock)

    first = service_tick.tick()
    second = service_tick.tick()

    assert first.dispatched == 1
    assert second.dispatched == 0
    with engine.connect() as connection:
        assert int(connection.scalar(text("SELECT count(*) FROM workflow_steps"))) == 2
        assert int(connection.scalar(text("SELECT count(*) FROM runs"))) == 2


def test_checkpoint_retention_keeps_latest_state_across_continuations(engine: Engine) -> None:
    service, _ = _services(engine, _Clock())
    workflow_id = _create(service, budget=WorkflowBudget(max_retained_checkpoints=1))
    for number in range(1, 4):
        _finish_step(
            engine, workflow_id, WorkflowStepResult(directive=next_directive(), state={"n": number})
        )
        detail = service.detail(1, workflow_id)
        assert len(detail.checkpoints) == 1
        assert detail.checkpoints[0].revision == number
        assert detail.checkpoints[0].state == {"n": number}
        assert _tick(engine, _Clock()).tick().dispatched == 1


def test_a_complete_directive_terminates_the_workflow(engine: Engine) -> None:
    clock = _Clock()
    service, _ = _services(engine, clock)
    workflow_id = _create(service)
    _finish_step(
        engine,
        workflow_id,
        WorkflowStepResult(directive=complete_directive(), state={"answer": "done"}),
    )

    detail = service.detail(1, workflow_id)
    assert detail.workflow.status is WorkflowStatus.SUCCEEDED
    assert detail.workflow.finished_at is not None
    assert _tick(engine, clock).tick().dispatched == 0


def test_a_time_wait_holds_no_run_and_wakes_on_its_instant(engine: Engine) -> None:
    clock = _Clock()
    service, _ = _services(engine, clock)
    workflow_id = _create(service)
    _finish_step(
        engine,
        workflow_id,
        WorkflowStepResult(directive=waiting_directive(NOW, seconds=300), state={"round": 1}),
    )

    waiting = service.detail(1, workflow_id).workflow
    assert waiting.status is WorkflowStatus.WAITING
    assert waiting.wait_kind is WorkflowWaitKind.TIME
    assert waiting.wakeup_at == NOW + timedelta(seconds=300)

    # A waiting workflow owns no Run, no Job and no Attempt: no execution capacity at all.
    with engine.connect() as connection:
        assert int(connection.scalar(text("SELECT count(*) FROM runs"))) == 1
        assert int(connection.scalar(text("SELECT count(*) FROM jobs"))) == 1
        assert int(connection.scalar(text("SELECT count(*) FROM job_attempts"))) == 1

    assert _tick(engine, clock).tick().dispatched == 0
    clock.advance(301)
    assert _tick(engine, clock).tick().dispatched == 1
    assert service.get(1, workflow_id).step_count == 2
    from nervos_core.infrastructure.database.workflows import SqlAlchemyWorkflowStepSource

    resumed = service.detail(1, workflow_id).steps[-1]
    assert resumed.run_id is not None
    snapshot = SqlAlchemyWorkflowStepSource(engine).snapshot_for_run(run_id=resumed.run_id)
    assert snapshot is not None
    assert snapshot.wake_signal == {}


def test_a_signal_wait_only_advances_on_the_owner_delivery(engine: Engine) -> None:
    clock = _Clock()
    service, _ = _services(engine, clock)
    workflow_id = _create(service)
    _finish_step(
        engine,
        workflow_id,
        WorkflowStepResult(directive=signal_directive("inbox_ready"), state={}),
    )
    assert service.get(1, workflow_id).wait_kind is WorkflowWaitKind.SIGNAL

    # A tick must never invent the signal a step asked for.
    assert _tick(engine, clock).tick().dispatched == 0

    wrong_key = service.signal(
        1, workflow_id, signal_key="other_key", payload={}, expected_revision=1
    )
    assert wrong_key.accepted is False and wrong_key.outcome == SIGNAL_NOT_WAITING

    stale = service.signal(
        1, workflow_id, signal_key="inbox_ready", payload={}, expected_revision=0
    )
    assert stale.accepted is False and stale.outcome == SIGNAL_STALE_REVISION

    accepted = service.signal(
        1, workflow_id, signal_key="inbox_ready", payload={"count": 3}, expected_revision=1
    )
    assert accepted.accepted is True and accepted.outcome == SIGNAL_ACCEPTED
    assert accepted.workflow.status is WorkflowStatus.RUNNABLE

    replay = service.signal(
        1, workflow_id, signal_key="inbox_ready", payload={"count": 3}, expected_revision=1
    )
    assert replay.accepted is True and replay.outcome == SIGNAL_REPLAYED

    with pytest.raises(WorkflowConflict):
        service.signal(
            1, workflow_id, signal_key="inbox_ready", payload={"count": 9}, expected_revision=1
        )
    assert _tick(engine, clock).tick().dispatched == 1
    assert service.get(1, workflow_id).step_count == 2


def test_accepted_signal_payload_is_delivered_only_to_its_resumed_step(engine: Engine) -> None:
    from nervos_core.infrastructure.database.workflows import SqlAlchemyWorkflowStepSource

    service, _ = _services(engine, _Clock())
    workflow_id = _create(service)
    _finish_step(
        engine, workflow_id, WorkflowStepResult(directive=signal_directive("go"), state={})
    )
    service.signal(1, workflow_id, signal_key="go", payload={"count": 3}, expected_revision=1)
    assert _tick(engine, _Clock()).tick().dispatched == 1
    run_id = service.detail(1, workflow_id).steps[-1].run_id
    assert run_id is not None
    snapshot = SqlAlchemyWorkflowStepSource(engine).snapshot_for_run(run_id=run_id)
    assert snapshot is not None
    assert snapshot.wake_signal == {"key": "go", "payload": {"count": 3}}
    _finish_step(engine, workflow_id, WorkflowStepResult(directive=next_directive(), state={}))
    assert _tick(engine, _Clock()).tick().dispatched == 1
    run_id = service.detail(1, workflow_id).steps[-1].run_id
    assert run_id is not None
    snapshot = SqlAlchemyWorkflowStepSource(engine).snapshot_for_run(run_id=run_id)
    assert snapshot is not None and snapshot.wake_signal == {}


def test_a_decision_wait_requires_a_decision_request() -> None:
    # A step that waits for an owner decision without proposing one would wait forever on
    # an approval that has nothing to bind to, so the shape is refused at construction.
    with pytest.raises(InvalidWorkflow):
        WorkflowStepResult(directive=decision_directive("send_reply"), state={})


def test_pause_stops_further_steps_and_resume_creates_no_signal(engine: Engine) -> None:
    clock = _Clock()
    service, _ = _services(engine, clock)
    workflow_id = _create(service)
    _finish_step(engine, workflow_id, WorkflowStepResult(directive=next_directive(), state={}))

    paused = service.pause(1, workflow_id)
    assert paused.paused is True
    assert paused.status is WorkflowStatus.RUNNABLE
    # The due-work scan leads with `paused`, so a paused workflow is never examined rather
    # than examined and refused -- the cheap shape, and the same observable outcome.
    report = _tick(engine, clock).tick()
    assert report.dispatched == 0 and report.examined == 0
    with engine.connect() as connection:
        assert int(connection.scalar(text("SELECT count(*) FROM runs"))) == 1

    resumed = service.resume(1, workflow_id)
    assert resumed.paused is False
    # Resuming clears the flag and nothing else: it must not fabricate a wakeup.
    assert resumed.wait_kind is None
    assert _tick(engine, clock).tick().dispatched == 1


def test_cancel_is_terminal_and_stops_continuations(engine: Engine) -> None:
    clock = _Clock()
    service, _ = _services(engine, clock)
    workflow_id = _create(service)
    _finish_step(engine, workflow_id, WorkflowStepResult(directive=next_directive(), state={}))

    cancelled = service.cancel(1, workflow_id)
    assert cancelled.status is WorkflowStatus.CANCELLED
    assert _tick(engine, clock).tick().dispatched == 0
    with pytest.raises(WorkflowTransitionError):
        service.pause(1, workflow_id)
    # An idempotent repeat stays cancelled rather than raising.
    assert service.cancel(1, workflow_id).status is WorkflowStatus.CANCELLED


def test_a_deadline_stops_further_dispatch(engine: Engine) -> None:
    clock = _Clock()
    service, _ = _services(engine, clock)
    workflow_id = _create(service, budget=WorkflowBudget(deadline_hours=1))
    _finish_step(engine, workflow_id, WorkflowStepResult(directive=next_directive(), state={}))

    clock.advance(3601)
    report = _tick(engine, clock).tick()

    assert report.failed == 1
    workflow = service.get(1, workflow_id)
    assert workflow.status is WorkflowStatus.FAILED
    assert workflow.review_reason == "deadline_exceeded"


def test_cumulative_reservations_survive_a_restart(engine: Engine) -> None:
    clock = _Clock()
    service, _ = _services(engine, clock)
    workflow_id = _create(service, budget=WorkflowBudget(model_call_reservation=2))
    first = reservation_for(LIMITS)
    assert first.model_calls == LIMITS.max_model_calls

    _finish_step(engine, workflow_id, WorkflowStepResult(directive=next_directive(), state={}))
    # A brand-new persistence object models a process restart: the counters are durable.
    restarted = SqlAlchemyWorkflowPersistence(engine)
    workflow = WorkflowService(restarted, clock=clock).get(1, workflow_id)
    assert workflow.reservations.model_calls == LIMITS.max_model_calls
    assert workflow.reservations.output_tokens == LIMITS.max_model_calls * LIMITS.max_output_tokens
    assert workflow.budget.model_call_reservation == 2


def test_a_changed_pinned_identity_becomes_needs_review(engine: Engine) -> None:
    clock = _Clock()
    service, _ = _services(engine, clock)
    workflow_id = _create(service)
    _finish_step(engine, workflow_id, WorkflowStepResult(directive=next_directive(), state={}))

    # The owner edits the instance: the continuation must not silently run on new settings.
    with engine.begin() as connection:
        connection.execute(text("UPDATE agent_instances SET model_name='other/model' WHERE id = 1"))

    report = _tick(engine, clock).tick()

    assert report.needs_review == 1
    workflow = service.get(1, workflow_id)
    assert workflow.status is WorkflowStatus.NEEDS_REVIEW
    assert workflow.review_reason == "configuration_changed"
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM runs")) == 1
        assert connection.scalar(text("SELECT count(*) FROM jobs")) == 1
    assert workflow.step_count == 1
    assert workflow.reservations.output_tokens == reservation_for(LIMITS).output_tokens
    # A workflow needing review never advances automatically again.
    assert _tick(engine, clock).tick().dispatched == 0


def test_a_disabled_instance_does_not_dispatch_but_is_not_failed(engine: Engine) -> None:
    clock = _Clock()
    service, _ = _services(engine, clock)
    workflow_id = _create(service)
    _finish_step(engine, workflow_id, WorkflowStepResult(directive=next_directive(), state={}))
    with engine.begin() as connection:
        connection.execute(text("UPDATE agent_instances SET enabled = 0 WHERE id = 1"))

    report = _tick(engine, clock).tick()

    assert report.dispatched == 0
    assert service.get(1, workflow_id).status is WorkflowStatus.RUNNABLE


def test_a_waiting_signal_or_decision_wait_is_never_released_by_a_tick(
    engine: Engine,
) -> None:
    clock = _Clock()
    _seed_tool_definition(engine)
    service, _ = _services(engine, clock)
    signal_id = _create(service, key="waiting-signal")
    _finish_step(
        engine,
        signal_id,
        WorkflowStepResult(directive=signal_directive("k1"), state={}),
    )
    decision_id = _create(service, key="waiting-decision")
    _finish_step(
        engine,
        decision_id,
        WorkflowStepResult(
            directive=decision_directive("k2"), state={}, decision_request=_proposal("k2", 0)
        ),
    )

    report = _tick(engine, clock).tick()
    assert report.dispatched == 0
    kinds = {w.wait_kind for w in service.list(1, limit=10, before_id=None)}
    assert kinds == {WorkflowWaitKind.SIGNAL, WorkflowWaitKind.OWNER_DECISION}


# ---------------------------------------------------------------------------------------
# W3: owner decisions
# ---------------------------------------------------------------------------------------


TOOL_DEFINITION_ID = 1


def _seed_tool_definition(engine: Engine) -> None:
    """One durable tool definition so a decision binds to a real reviewed tool identity.

    A built-in source needs no `mcp_connections` row, which keeps this fixture free of any
    account or credential setup.
    """
    with engine.begin() as connection:
        existing = int(
            connection.scalar(
                text("SELECT count(*) FROM tool_definitions WHERE upstream_name = 'send_reply'")
            )
        )
        if existing:
            return
        connection.execute(
            text(
                "INSERT INTO tool_definitions"
                "(source_kind,source_id,upstream_name,model_name,display_name,description,"
                "input_schema,output_schema,fingerprint,status,created_at,updated_at)"
                " VALUES('builtin',NULL,'send_reply','send_reply','Send reply','',"
                "'{}',NULL,:f,'available',:n,:n)"
            ),
            {"f": "c" * 64, "n": NOW},
        )


def _proposal(upstream_name: str, checkpoint_revision: int):
    """A bounded, exactly-bound proposed action, as a step would build it."""
    from nervos_core.domain.workflows import (
        WorkflowDecisionRequest,
        decision_arguments_digest,
    )

    arguments = {"to": "ops@example.invalid", "subject": "Weekly status"}
    return WorkflowDecisionRequest(
        checkpoint_revision=checkpoint_revision,
        tool_definition_id=TOOL_DEFINITION_ID,
        upstream_name=upstream_name,
        action_fingerprint="a" * 64,
        arguments=arguments,
        arguments_digest=decision_arguments_digest(arguments),
        preview={"to": "ops@example.invalid"},
        package_content_digest=None,
        effective_config_digest=None,
        expires_at=NOW + timedelta(hours=1),
    )


def _request_decision(engine: Engine, workflow_id: int) -> int:
    """Commit a decision row the way the fenced Worker boundary does for a real proposal."""
    _seed_tool_definition(engine)
    service, _ = _services(engine, _Clock())
    persistence = SqlAlchemyJobExecutionPersistence(
        engine, workflow_steps=SqlAlchemyWorkflowStepFinalizer()
    )
    claim = _claim_and_start(engine, workflow_id)
    assert (
        persistence.succeed(
            claim,
            output_text="proposed",
            finish_reason="stop",
            usage=_usage(),
            elapsed_ms=5,
            now=NOW,
            workflow_step=WorkflowStepResult(
                directive=decision_directive("send_reply"),
                state={},
                decision_request=_proposal("send_reply", 0),
            ),
        )
        is True
    )
    decisions = service.list_decisions(1, workflow_id)
    assert len(decisions) == 1
    return decisions[0].id


def test_an_owner_decision_is_recorded_and_releases_its_wait(engine: Engine) -> None:
    clock = _Clock()
    service, _ = _services(engine, clock)
    workflow_id = _create(service)
    decision_id = _request_decision(engine, workflow_id)

    waiting = service.get(1, workflow_id)
    assert waiting.status is WorkflowStatus.WAITING
    assert waiting.wait_kind is WorkflowWaitKind.OWNER_DECISION
    # A tick cannot approve for the owner.
    assert _tick(engine, clock).tick().dispatched == 0

    decided = service.decide(1, workflow_id, decision_id, approve=True, expected_revision=0)
    assert decided.state is WorkflowDecisionState.APPROVED
    assert service.get(1, workflow_id).status is WorkflowStatus.RUNNABLE
    # It still grants nothing: the next step re-checks live authority at dispatch.
    assert _tick(engine, clock).tick().dispatched == 1


def test_approved_decision_cannot_dispatch_after_its_expiry(engine: Engine) -> None:
    clock = _Clock()
    service, _ = _services(engine, clock)
    workflow_id = _create(service)
    decision_id = _request_decision(engine, workflow_id)
    service.decide(1, workflow_id, decision_id, approve=True, expected_revision=0)
    clock.advance(3601)
    assert _tick(engine, clock).tick().dispatched == 0
    assert service.get(1, workflow_id).status is WorkflowStatus.NEEDS_REVIEW
    assert service.list_decisions(1, workflow_id)[0].state is WorkflowDecisionState.EXPIRED


def test_approved_decision_is_consumed_with_the_continuation(engine: Engine) -> None:
    clock = _Clock()
    service, _ = _services(engine, clock)
    workflow_id = _create(service)
    decision_id = _request_decision(engine, workflow_id)
    service.decide(1, workflow_id, decision_id, approve=True, expected_revision=0)
    assert _tick(engine, clock).tick().dispatched == 1
    assert service.list_decisions(1, workflow_id)[0].consumed_at == NOW


def test_a_decision_at_the_wrong_revision_is_refused(engine: Engine) -> None:
    clock = _Clock()
    service, _ = _services(engine, clock)
    workflow_id = _create(service)
    decision_id = _request_decision(engine, workflow_id)

    with pytest.raises(WorkflowConflict):
        service.decide(1, workflow_id, decision_id, approve=True, expected_revision=99)
    assert service.list_decisions(1, workflow_id)[0].state is WorkflowDecisionState.PENDING


def test_declined_decision_never_dispatches_proposed_work(engine: Engine) -> None:
    service, _ = _services(engine, _Clock())
    workflow_id = _create(service)
    decision = _request_decision(engine, workflow_id)
    service.decide(1, workflow_id, decision, approve=False, expected_revision=0)
    assert service.get(1, workflow_id).status is WorkflowStatus.CANCELLED
    assert _tick(engine, _Clock()).tick().dispatched == 0


def test_parent_cancellation_does_not_strand_active_run_completion(engine: Engine) -> None:
    service, _ = _services(engine, _Clock())
    workflow_id = _create(service)
    claim = _claim_and_start(engine, workflow_id)
    service.cancel(1, workflow_id)
    persistence = SqlAlchemyJobExecutionPersistence(
        engine, workflow_steps=SqlAlchemyWorkflowStepFinalizer()
    )
    assert persistence.succeed(
        claim,
        output_text="finished active step",
        finish_reason="stop",
        usage=_usage(),
        elapsed_ms=1,
        now=NOW,
        workflow_step=WorkflowStepResult(directive=next_directive(), state={}),
    )
    assert service.get(1, workflow_id).status is WorkflowStatus.CANCELLED
    assert _tick(engine, _Clock()).tick().dispatched == 0


def test_signal_wait_prefix_does_not_hide_runnable_work(engine: Engine) -> None:
    service, _ = _services(engine, _Clock())
    blocked = _create(service, key="blocked")
    _finish_step(engine, blocked, WorkflowStepResult(directive=signal_directive("go"), state={}))
    ready = _create(service, key="ready")
    _finish_step(engine, ready, WorkflowStepResult(directive=next_directive(), state={}))
    persistence = SqlAlchemyWorkflowContinuationPersistence(
        engine, _builtin_resolver(engine), scan_limit=1, dispatch_limit=1
    )
    report = persistence.tick(now=NOW)
    assert report.dispatched == 1
    assert service.get(1, blocked).status is WorkflowStatus.WAITING
    assert service.get(1, ready).step_count == 2


def test_initial_multi_call_token_reservation_is_enforced_before_run_creation(
    engine: Engine,
) -> None:
    service, _ = _services(engine, _Clock())
    with pytest.raises(WorkflowConflict, match="reservation"):
        _create(service, budget=WorkflowBudget(output_token_reservation=1))
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM runs")) == 0


def test_a_decision_is_refused_for_another_owner_and_after_being_used(engine: Engine) -> None:
    clock = _Clock()
    service, _ = _services(engine, clock)
    workflow_id = _create(service)
    decision_id = _request_decision(engine, workflow_id)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users(username,password_hash,role,is_active,created_at,updated_at)"
                " VALUES('other',X'00','admin',1,:n,:n)"
            ),
            {"n": NOW},
        )

    with pytest.raises(WorkflowNotFound):
        service.decide(2, workflow_id, decision_id, approve=True, expected_revision=0)

    assert (
        service.decide(1, workflow_id, decision_id, approve=False, expected_revision=0).state
        is WorkflowDecisionState.DENIED
    )
    with pytest.raises(WorkflowConflict):
        service.decide(1, workflow_id, decision_id, approve=True, expected_revision=0)


def test_cancelling_a_workflow_makes_its_pending_decision_undecidable(engine: Engine) -> None:
    clock = _Clock()
    service, _ = _services(engine, clock)
    workflow_id = _create(service)
    decision_id = _request_decision(engine, workflow_id)

    service.cancel(1, workflow_id)

    assert service.list_decisions(1, workflow_id)[0].state is WorkflowDecisionState.CANCELLED
    with pytest.raises(WorkflowConflict):
        service.decide(1, workflow_id, decision_id, approve=True, expected_revision=0)


# ---------------------------------------------------------------------------------------
# Bound and version contracts
# ---------------------------------------------------------------------------------------


def test_the_workflow_budget_refuses_values_outside_the_frozen_bounds() -> None:
    with pytest.raises(InvalidWorkflow):
        WorkflowBudget(max_steps=129)
    with pytest.raises(InvalidWorkflow):
        WorkflowBudget(deadline_hours=169)
    with pytest.raises(InvalidWorkflow):
        WorkflowBudget(model_call_reservation=2049)
    with pytest.raises(InvalidWorkflow):
        WorkflowBudget(max_retained_checkpoints=129)


def test_directive_exclusivity_is_enforced_at_construction() -> None:
    from nervos_core.domain.workflows import WorkflowDirective

    # A directive that both finished and scheduled something has no honest single reading.
    with pytest.raises(InvalidWorkflow):
        WorkflowDirective(WorkflowDirectiveKind.COMPLETE, wait_seconds=5)
    with pytest.raises(InvalidWorkflow):
        WorkflowDirective(WorkflowDirectiveKind.NEXT, signal_key="k")
    # A time wait that also names a signal would let one wait mean two things.
    with pytest.raises(InvalidWorkflow):
        WorkflowDirective(
            WorkflowDirectiveKind.WAIT,
            wait_kind=WorkflowWaitKind.TIME,
            signal_key="k",
        )
    # A time wait needs exactly one way to name its instant.
    with pytest.raises(InvalidWorkflow):
        WorkflowDirective(WorkflowDirectiveKind.WAIT, wait_kind=WorkflowWaitKind.TIME)
    with pytest.raises(InvalidWorkflow):
        WorkflowDirective(
            WorkflowDirectiveKind.WAIT,
            wait_kind=WorkflowWaitKind.TIME,
            wait_seconds=30,
            wakeup_at=NOW,
        )


def test_a_waiting_workflow_without_a_wait_reason_cannot_be_constructed() -> None:
    from nervos_core.domain.workflows import WorkflowExecution, WorkflowReservations

    def build(**overrides: object) -> WorkflowExecution:
        values: dict[str, object] = {
            "id": 1,
            "owner_user_id": 1,
            "agent_instance_id": 1,
            "workflow_kind": "research",
            "state_schema_version": 1,
            "submission_key": "k",
            "submission_digest": "b" * 64,
            "status": WorkflowStatus.WAITING,
            "paused": False,
            "checkpoint_revision": 0,
            "step_count": 0,
            "budget": WorkflowBudget(),
            "reservations": WorkflowReservations(),
            "deadline_at": NOW,
        }
        values.update(overrides)
        return WorkflowExecution(**values)  # type: ignore[arg-type]

    with pytest.raises(InvalidWorkflow):
        build()
    with pytest.raises(InvalidWorkflow):
        build(status=WorkflowStatus.RUNNING, wait_kind=WorkflowWaitKind.TIME, wakeup_at=NOW)
    assert build(wait_kind=WorkflowWaitKind.SIGNAL, signal_key="k").status is (
        WorkflowStatus.WAITING
    )
