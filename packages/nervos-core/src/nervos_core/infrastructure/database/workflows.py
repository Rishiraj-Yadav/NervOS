"""W1-W3 durable workflow persistence (ADR 0039).

Three responsibilities, all through the one short `BEGIN IMMEDIATE` transaction primitive:

* :class:`SqlAlchemyWorkflowPersistence` -- the owner-scoped control plane, plus the
  :class:`SqlAlchemyWorkflowStepFinalizer` the fenced Worker boundary calls **on its own
  connection** so a step's Run success and its checkpoint are one transaction.
* :meth:`create_workflow_with_step_on_connection` -- creation, and the continuation writer
  that runs beside the canonical `insert_run_and_job_on_connection`.

Every continuation goes through `insert_run_and_job_on_connection`. There is no second
Run-insertion path, no second queue, and no way for a workflow to bypass admission,
fairness, the grant cutoff, or the execution snapshot.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Protocol, cast

from sqlalchemy import Connection, Engine, delete, func, insert, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError

from nervos_core.application.agents import DurableSubmissionRejected
from nervos_core.application.errors import PersistenceUnavailable, QueueCapacityExceeded
from nervos_core.application.workflows import (
    SIGNAL_ACCEPTED,
    SIGNAL_CANCELLED,
    SIGNAL_NOT_WAITING,
    SIGNAL_REPLAYED,
    SIGNAL_STALE_REVISION,
    WORKFLOW_TICK_DISPATCH_LIMIT,
    WORKFLOW_TICK_SCAN_LIMIT,
    WorkflowCapacityExceeded,
    WorkflowCreationRequest,
    WorkflowDecisionView,
    WorkflowDetail,
    WorkflowSignalOutcome,
    WorkflowSignalView,
    WorkflowStepSnapshot,
    WorkflowStepView,
    WorkflowTickReport,
    reservation_for,
    step_input_text,
)
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.runs import Run, RunLimits, RunStatus
from nervos_core.domain.tools import JsonValue, canonical_json_text, require_json_value
from nervos_core.domain.workflows import (
    DECIDABLE_DECISION_STATES,
    INVALID_REVIEW_REASONS,
    REVIEW_AUTHORITY_LOST,
    REVIEW_BUDGET_EXHAUSTED,
    REVIEW_CONFIGURATION_CHANGED,
    REVIEW_DEADLINE_EXCEEDED,
    REVIEW_RUN_TERMINAL,
    REVIEW_STEP_LIMIT,
    TERMINAL_WORKFLOW_STATUSES,
    WORKFLOW_JOB_MAX_ATTEMPTS,
    WORKFLOW_STATE_SCHEMA_VERSION,
    FrozenJSONValue,
    FrozenState,
    WorkflowBudget,
    WorkflowCheckpoint,
    WorkflowConflict,
    WorkflowDecisionState,
    WorkflowExecution,
    WorkflowNotFound,
    WorkflowNotLinked,
    WorkflowReservations,
    WorkflowStatus,
    WorkflowStep,
    WorkflowStepResult,
    WorkflowStepStatus,
    WorkflowTransitionError,
    WorkflowWaitKind,
    accepts_reservation,
    assert_transition,
    content_digest,
    decision_arguments_digest,
    freeze_state,
    resolved_wakeup_at,
    state_digest,
    thaw_state,
)
from nervos_core.infrastructure.database.jobs import insert_run_and_job_on_connection
from nervos_core.infrastructure.database.models import (
    AgentInstanceRecord,
    JobRecord,
    RunRecord,
    WorkflowCheckpointRecord,
    WorkflowDecisionRecord,
    WorkflowExecutionRecord,
    WorkflowSignalRecord,
    WorkflowStepRecord,
)
from nervos_core.infrastructure.database.transaction import TransactionRunner

_ACTIVE_STATUSES = tuple(
    status.value
    for status in (
        WorkflowStatus.PENDING,
        WorkflowStatus.RUNNABLE,
        WorkflowStatus.RUNNING,
        WorkflowStatus.WAITING,
    )
)


class _Uncertain(Exception):
    """A uniqueness or fence conflict the caller reconciles by reading durable identity."""


class WorkflowLimitsResolver(Protocol):
    """Resolve one pinned agent definition's per-step Run limits.

    Injected rather than reconstructed here, because a package's declared limits live in
    the definition resolver the Scheduler already composes. A refusal to resolve is a
    configuration change, which is `needs_review` -- never an invented default.
    """

    def __call__(self, definition_id: AgentDefinitionId) -> RunLimits: ...


# ---------------------------------------------------------------------------------------
# Row mapping
# ---------------------------------------------------------------------------------------


def _execution_from_row(row: RowMapping) -> WorkflowExecution:
    return WorkflowExecution(
        id=int(row["id"]),
        owner_user_id=int(row["owner_user_id"]),
        agent_instance_id=int(row["agent_instance_id"]),
        workflow_kind=str(row["workflow_kind"]),
        state_schema_version=int(row["state_schema_version"]),
        submission_key=str(row["submission_key"]),
        submission_digest=str(row["submission_digest"]),
        status=WorkflowStatus(str(row["status"])),
        paused=bool(row["paused"]),
        checkpoint_revision=int(row["checkpoint_revision"]),
        step_count=int(row["step_count"]),
        budget=WorkflowBudget(
            max_steps=int(row["max_steps"]),
            model_call_reservation=int(row["model_call_reservation"]),
            tool_call_reservation=int(row["tool_call_reservation"]),
            output_token_reservation=int(row["output_token_reservation"]),
            deadline_hours=int(row["deadline_hours"]),
            max_retained_checkpoints=int(row["max_retained_checkpoints"]),
        ),
        reservations=WorkflowReservations(
            model_calls=int(row["reserved_model_calls"]),
            tool_calls=int(row["reserved_tool_calls"]),
            output_tokens=int(row["reserved_output_tokens"]),
        ),
        deadline_at=row["deadline_at"],
        wait_kind=WorkflowWaitKind(str(row["wait_kind"])) if row["wait_kind"] else None,
        wakeup_at=row["wakeup_at"],
        signal_key=row["signal_key"],
        decision_key=row["decision_key"],
        review_reason=row["review_reason"],
        agent_key=str(row["agent_key"]),
        agent_definition_version=str(row["agent_definition_version"]),
        model_provider=str(row["model_provider"]),
        model_name=str(row["model_name"]),
        package_content_digest=row["package_content_digest"],
        package_environment_digest=row["package_environment_digest"],
        effective_config_digest=row["effective_config_digest"],
        agent_instance_config_revision=row["agent_instance_config_revision"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        finished_at=row["finished_at"],
    )


def _step_from_row(row: RowMapping) -> WorkflowStep:
    state = require_json_value(_loads(row["state_json"]))
    return WorkflowStep(
        id=int(row["id"]),
        workflow_id=int(row["workflow_id"]),
        step_number=int(row["step_number"]),
        run_id=int(row["run_id"]) if row["run_id"] is not None else None,
        expected_checkpoint_revision=int(row["expected_checkpoint_revision"]),
        state_version=int(row["state_schema_version"]),
        state=freeze_state(state),
        state_digest=str(row["state_digest"]),
        status=WorkflowStepStatus(str(row["status"])),
        summary=row["summary"],
        created_at=row["created_at"],
        finished_at=row["finished_at"],
    )


def _checkpoint_from_row(row: RowMapping) -> WorkflowCheckpoint:
    return WorkflowCheckpoint(
        id=int(row["id"]),
        workflow_id=int(row["workflow_id"]),
        revision=int(row["revision"]),
        step_number=int(row["step_number"]),
        state=freeze_state(require_json_value(_loads(row["state_json"]))),
        state_digest=str(row["state_digest"]),
        created_at=row["created_at"],
    )


def _decision_from_row(row: RowMapping) -> WorkflowDecisionView:
    return WorkflowDecisionView(
        id=int(row["id"]),
        checkpoint_revision=int(row["checkpoint_revision"]),
        tool_definition_id=int(row["tool_definition_id"]),
        upstream_name=str(row["upstream_name"]),
        arguments_digest=str(row["arguments_digest"]),
        preview=freeze_state(require_json_value(_loads(row["preview_json"]))),
        state=WorkflowDecisionState(str(row["state"])),
        requested_at=row["requested_at"],
        expires_at=row["expires_at"],
        decided_at=row["decided_at"],
        consumed_at=row["consumed_at"],
    )


def _signal_from_row(row: RowMapping) -> WorkflowSignalView:
    return WorkflowSignalView(
        id=int(row["id"]),
        signal_key=str(row["signal_key"]),
        expected_revision=int(row["expected_revision"]),
        outcome=str(row["outcome"]),
        received_at=row["received_at"],
        accepted_at=row["accepted_at"],
    )


def _loads(text: str) -> object:
    return json.loads(text)


def _thaw(value: FrozenJSONValue) -> JsonValue:
    return thaw_state(value)


def _frozen_to_json(value: Mapping[str, FrozenJSONValue]) -> str:
    return canonical_json_text(thaw_state(value))


# ---------------------------------------------------------------------------------------
# Creation and continuation
# ---------------------------------------------------------------------------------------


def create_workflow_with_step_on_connection(
    connection: Connection,
    request: WorkflowCreationRequest,
) -> WorkflowExecution:
    """Insert one workflow, its first step, and that step's Run + Job, in the caller's
    transaction.

    Replays an identical request through the unique `(owner, submission_key)` constraint
    rather than through a pre-flight read, so two concurrent identical submissions cannot
    both insert and a changed body under the same key is a conflict rather than a second
    workflow.
    """
    digest = content_digest(
        {
            "workflow_kind": request.workflow_kind,
            "agent_instance_id": request.agent_instance_id,
            "input_text": request.input_text,
            "max_steps": request.budget.max_steps,
            "model_calls": request.budget.model_call_reservation,
            "tool_calls": request.budget.tool_call_reservation,
            "output_tokens": request.budget.output_token_reservation,
            "deadline_hours": request.budget.deadline_hours,
        }
    )
    existing = (
        connection.execute(
            select(WorkflowExecutionRecord).where(
                WorkflowExecutionRecord.owner_user_id == request.owner_user_id,
                WorkflowExecutionRecord.submission_key == request.submission_key,
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        if str(existing["submission_digest"]) != digest:
            raise WorkflowConflict("submission identity already exists with different content")
        return _execution_from_row(existing)

    active = int(
        connection.execute(
            select(func.count())
            .select_from(WorkflowExecutionRecord)
            .where(
                WorkflowExecutionRecord.owner_user_id == request.owner_user_id,
                WorkflowExecutionRecord.status.in_(_ACTIVE_STATUSES),
            )
        ).scalar_one()
    )
    if active >= request.max_active:
        raise WorkflowCapacityExceeded("owner already has the maximum active workflows")
    if not accepts_reservation(
        WorkflowReservations(), request.budget, reservation_for(request.limits)
    ):
        raise WorkflowConflict("initial step exceeds cumulative reservation")

    run = insert_run_and_job_on_connection(
        connection,
        owner_user_id=request.owner_user_id,
        agent_instance_id=request.agent_instance_id,
        input_text=request.input_text,
        limits=request.limits,
        definition_id=request.definition_id,
        now=request.now,
        max_attempts=WORKFLOW_JOB_MAX_ATTEMPTS,
        capacity=request.capacity,
        agent_capacity=request.agent_capacity,
        provider_capacity=request.provider_capacity,
    )
    snapshot = run.executable
    workflow_insert = connection.execute(
        insert(WorkflowExecutionRecord).values(
            owner_user_id=request.owner_user_id,
            agent_instance_id=request.agent_instance_id,
            workflow_kind=request.workflow_kind,
            state_schema_version=WORKFLOW_STATE_SCHEMA_VERSION,
            submission_key=request.submission_key,
            submission_digest=digest,
            status=WorkflowStatus.RUNNING.value,
            paused=False,
            checkpoint_revision=0,
            step_count=1,
            max_steps=request.budget.max_steps,
            model_call_reservation=request.budget.model_call_reservation,
            tool_call_reservation=request.budget.tool_call_reservation,
            output_token_reservation=request.budget.output_token_reservation,
            reserved_model_calls=request.limits.max_model_calls,
            reserved_tool_calls=request.limits.max_tool_calls,
            reserved_output_tokens=reservation_for(request.limits).output_tokens,
            deadline_hours=request.budget.deadline_hours,
            max_retained_checkpoints=request.budget.max_retained_checkpoints,
            deadline_at=request.budget.deadline_at(request.now),
            agent_key=run.agent_key,
            agent_definition_version=run.agent_definition_version,
            model_provider=run.model_provider,
            model_name=run.model_name,
            package_content_digest=snapshot.package_content_digest,
            package_environment_digest=snapshot.package_environment_digest,
            effective_config_digest=snapshot.effective_config_digest,
            agent_instance_config_revision=snapshot.agent_instance_config_revision,
            created_at=request.now,
            updated_at=request.now,
        )
    )
    workflow_id = _inserted_id(workflow_insert)
    connection.execute(
        insert(WorkflowStepRecord).values(
            workflow_id=workflow_id,
            step_number=1,
            run_id=run.id,
            expected_checkpoint_revision=0,
            state_schema_version=WORKFLOW_STATE_SCHEMA_VERSION,
            state_json="{}",
            state_digest=state_digest(freeze_state({})),
            status=WorkflowStepStatus.RUNNING.value,
            created_at=request.now,
        )
    )
    return _reload_execution(connection, workflow_id)


def submit_continuation_on_connection(
    connection: Connection,
    *,
    workflow: WorkflowExecution,
    step_number: int,
    checkpoint_state: FrozenState,
    expected_revision: int,
    limits: RunLimits,
    now: datetime,
    capacity: int,
    agent_capacity: int,
    provider_capacity: int,
) -> int:
    """Submit one continuation Run + Job, link the step, and draw its reservation, atomically.

    Everything the next dispatch needs is verified on *this* connection, so two Schedulers
    racing the same workflow serialize on the write lock and the loser reads an already-linked
    step rather than inserting a second Run.
    """
    current = _reload_execution(connection, workflow.id)
    if current.checkpoint_revision != expected_revision:
        raise _Uncertain("checkpoint revision moved")
    linked = connection.execute(
        select(WorkflowStepRecord.run_id).where(
            WorkflowStepRecord.workflow_id == current.id,
            WorkflowStepRecord.step_number == step_number,
        )
    ).one_or_none()
    if linked is not None:
        if linked[0] is None:
            raise _Uncertain("step exists without a Run link")
        return int(linked[0])

    claim = reservation_for(limits)
    if not accepts_reservation(current.reservations, current.budget, claim):
        raise _Uncertain("cumulative reservation exhausted")
    if current.step_count + 1 > current.budget.max_steps:
        raise _Uncertain("step budget exhausted")

    # The caller converts refusals into durable needs_review in this transaction.
    # Roll back the entire canonical insertion before that conversion can commit.
    with connection.begin_nested():
        return _insert_continuation(
            connection,
            current,
            step_number,
            checkpoint_state,
            expected_revision,
            limits,
            now,
            capacity,
            agent_capacity,
            provider_capacity,
        )


def _insert_continuation(
    connection: Connection,
    current: WorkflowExecution,
    step_number: int,
    checkpoint_state: FrozenState,
    expected_revision: int,
    limits: RunLimits,
    now: datetime,
    capacity: int,
    agent_capacity: int,
    provider_capacity: int,
) -> int:
    claim = reservation_for(limits)
    run = insert_run_and_job_on_connection(
        connection,
        owner_user_id=current.owner_user_id,
        agent_instance_id=current.agent_instance_id,
        input_text=step_input_text(current.workflow_kind, step_number, expected_revision),
        limits=limits,
        definition_id=None,
        now=now,
        max_attempts=WORKFLOW_JOB_MAX_ATTEMPTS,
        capacity=capacity,
        agent_capacity=agent_capacity,
        provider_capacity=provider_capacity,
    )
    _assert_identity_unchanged(connection, current, run)
    connection.execute(
        insert(WorkflowStepRecord).values(
            workflow_id=current.id,
            step_number=step_number,
            run_id=run.id,
            expected_checkpoint_revision=expected_revision,
            state_schema_version=WORKFLOW_STATE_SCHEMA_VERSION,
            state_json=_frozen_to_json(checkpoint_state),
            state_digest=state_digest(checkpoint_state),
            status=WorkflowStepStatus.RUNNING.value,
            created_at=now,
        )
    )
    connection.execute(
        update(WorkflowExecutionRecord)
        .where(WorkflowExecutionRecord.id == current.id)
        .values(
            status=WorkflowStatus.RUNNING.value,
            wait_kind=None,
            wakeup_at=None,
            signal_key=None,
            decision_key=None,
            step_count=current.step_count + 1,
            reserved_model_calls=current.reservations.model_calls + claim.model_calls,
            reserved_tool_calls=current.reservations.tool_calls + claim.tool_calls,
            reserved_output_tokens=current.reservations.output_tokens + claim.output_tokens,
            updated_at=now,
        )
    )
    return run.id


def _assert_identity_unchanged(
    connection: Connection, workflow: WorkflowExecution, run: Run
) -> None:
    """Refuse a continuation whose pinned executable identity has moved.

    A changed package, configuration or model is `needs_review`, never an automatic re-pin:
    silently continuing on the new artifact would execute the remaining steps of an
    application against code the owner never approved for it.
    """

    assert isinstance(run, Run)
    snapshot = run.executable
    if (
        workflow.agent_key != run.agent_key
        or workflow.agent_definition_version != run.agent_definition_version
        or workflow.model_provider != run.model_provider
        or workflow.model_name != run.model_name
        or workflow.package_content_digest != snapshot.package_content_digest
        or workflow.package_environment_digest != snapshot.package_environment_digest
        or workflow.effective_config_digest != snapshot.effective_config_digest
        or workflow.agent_instance_config_revision != snapshot.agent_instance_config_revision
    ):
        raise _Uncertain("pinned executable identity changed")


def _inserted_id(result: object) -> int:
    key = getattr(result, "inserted_primary_key", None)
    if key is None or key[0] is None:
        raise PersistenceUnavailable
    return int(key[0])


def _reload_execution(connection: Connection, workflow_id: int) -> WorkflowExecution:
    row = (
        connection.execute(
            select(WorkflowExecutionRecord).where(WorkflowExecutionRecord.id == workflow_id)
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise WorkflowNotFound
    return _execution_from_row(row)


# ---------------------------------------------------------------------------------------
# Owner-scoped control plane
# ---------------------------------------------------------------------------------------


class SqlAlchemyWorkflowPersistence:
    """Owner-scoped durable workflow control plane."""

    def __init__(
        self,
        engine: Engine,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._engine = engine
        self._runner = TransactionRunner(engine, sleep)

    def create_workflow(self, request: WorkflowCreationRequest) -> WorkflowExecution:
        try:
            return self._runner.run(
                lambda connection: create_workflow_with_step_on_connection(connection, request)
            )
        except IntegrityError as error:
            # A concurrent identical submission won the unique constraint. Reconciliation is
            # a read of durable identity, never a second insert.
            existing = self._runner.run(lambda connection: _replay_or_conflict(connection, request))
            if existing is not None:
                return existing
            raise PersistenceUnavailable from error

    def get_workflow(self, *, owner_user_id: int, workflow_id: int) -> WorkflowExecution:
        return self._runner.run(
            lambda connection: _owned_execution(connection, owner_user_id, workflow_id)
        )

    def list_workflows(
        self, *, owner_user_id: int, limit: int, before_id: int | None
    ) -> tuple[WorkflowExecution, ...]:
        def operation(connection: Connection) -> tuple[WorkflowExecution, ...]:
            statement = select(WorkflowExecutionRecord).where(
                WorkflowExecutionRecord.owner_user_id == owner_user_id
            )
            if before_id is not None:
                statement = statement.where(WorkflowExecutionRecord.id < before_id)
            rows = (
                connection.execute(
                    statement.order_by(WorkflowExecutionRecord.id.desc()).limit(limit)
                )
                .mappings()
                .all()
            )
            return tuple(_execution_from_row(row) for row in rows)

        return self._runner.run(operation)

    def count_active(self, *, owner_user_id: int) -> int:
        def operation(connection: Connection) -> int:
            return int(
                connection.execute(
                    select(func.count())
                    .select_from(WorkflowExecutionRecord)
                    .where(
                        WorkflowExecutionRecord.owner_user_id == owner_user_id,
                        WorkflowExecutionRecord.status.in_(_ACTIVE_STATUSES),
                    )
                ).scalar_one()
            )

        return self._runner.run(operation)

    def detail(self, *, owner_user_id: int, workflow_id: int) -> WorkflowDetail:
        def operation(connection: Connection) -> WorkflowDetail:
            workflow = _owned_execution(connection, owner_user_id, workflow_id)
            # Explicit labelled columns rather than an ORM entity plus joins: with two outer joins a
            # plain entity select is ambiguous about which `status` is whose, and a step row
            # silently reporting its Job's status as its own would be a durable lie.
            step_columns = (
                "id",
                "workflow_id",
                "step_number",
                "run_id",
                "expected_checkpoint_revision",
                "state_schema_version",
                "state_json",
                "state_digest",
                "status",
                "summary",
                "created_at",
                "finished_at",
            )
            statement = (
                select(
                    *(
                        getattr(WorkflowStepRecord, name).label(f"step_{name}")
                        for name in step_columns
                    ),
                    RunRecord.status.label("run_status"),
                    JobRecord.status.label("job_phase"),
                )
                .outerjoin(RunRecord, RunRecord.id == WorkflowStepRecord.run_id)
                .outerjoin(JobRecord, JobRecord.run_id == RunRecord.id)
            )
            steps = (
                connection.execute(
                    statement.where(WorkflowStepRecord.workflow_id == workflow_id).order_by(
                        WorkflowStepRecord.step_number.asc()
                    )
                )
                .mappings()
                .all()
            )
            views = tuple(
                WorkflowStepView(
                    step=_step_from_row(
                        cast("RowMapping", {name: row[f"step_{name}"] for name in step_columns})
                    ),
                    run_id=row["step_run_id"],
                    run_status=row["run_status"],
                    job_phase=row["job_phase"],
                )
                for row in steps
            )
            checkpoints = tuple(
                _checkpoint_from_row(row)
                for row in connection.execute(
                    select(WorkflowCheckpointRecord)
                    .where(WorkflowCheckpointRecord.workflow_id == workflow_id)
                    .order_by(WorkflowCheckpointRecord.revision.asc())
                )
                .mappings()
                .all()
            )
            decisions = tuple(
                _decision_from_row(row)
                for row in connection.execute(
                    select(WorkflowDecisionRecord)
                    .where(WorkflowDecisionRecord.workflow_id == workflow_id)
                    .order_by(WorkflowDecisionRecord.id.asc())
                )
                .mappings()
                .all()
            )
            signals = tuple(
                _signal_from_row(row)
                for row in connection.execute(
                    select(WorkflowSignalRecord)
                    .where(WorkflowSignalRecord.workflow_id == workflow_id)
                    .order_by(WorkflowSignalRecord.id.asc())
                )
                .mappings()
                .all()
            )
            return WorkflowDetail(
                workflow=workflow,
                steps=views,
                checkpoints=checkpoints,
                decisions=decisions,
                signals=signals,
            )

        return self._runner.run(operation)

    def set_paused(
        self, *, owner_user_id: int, workflow_id: int, paused: bool, now: datetime
    ) -> WorkflowExecution:
        def operation(connection: Connection) -> WorkflowExecution:
            workflow = _owned_execution(connection, owner_user_id, workflow_id)
            if workflow.status in TERMINAL_WORKFLOW_STATUSES:
                raise WorkflowTransitionError("a terminal workflow cannot be paused or resumed")
            updated = connection.execute(
                update(WorkflowExecutionRecord)
                .where(
                    WorkflowExecutionRecord.id == workflow_id,
                    WorkflowExecutionRecord.owner_user_id == owner_user_id,
                )
                .values(paused=paused, updated_at=now)
            )
            if updated.rowcount != 1:
                raise _Uncertain("pause flag moved")
            return _reload_execution(connection, workflow_id)

        return self._runner.run(operation)

    def cancel(self, *, owner_user_id: int, workflow_id: int, now: datetime) -> WorkflowExecution:
        def operation(connection: Connection) -> WorkflowExecution:
            workflow = _owned_execution(connection, owner_user_id, workflow_id)
            if workflow.status is WorkflowStatus.CANCELLED:
                return workflow
            assert_transition(workflow.status, WorkflowStatus.CANCELLED)
            connection.execute(
                update(WorkflowExecutionRecord)
                .where(
                    WorkflowExecutionRecord.id == workflow_id,
                    WorkflowExecutionRecord.owner_user_id == owner_user_id,
                )
                .values(
                    status=WorkflowStatus.CANCELLED.value,
                    wait_kind=None,
                    wakeup_at=None,
                    signal_key=None,
                    decision_key=None,
                    updated_at=now,
                    finished_at=now,
                )
            )
            # Any pending decision stops being decidable the moment the workflow does.
            connection.execute(
                update(WorkflowDecisionRecord)
                .where(
                    WorkflowDecisionRecord.workflow_id == workflow_id,
                    WorkflowDecisionRecord.state == WorkflowDecisionState.PENDING.value,
                )
                .values(state=WorkflowDecisionState.CANCELLED.value, decided_at=now)
            )
            return _reload_execution(connection, workflow_id)

        return self._runner.run(operation)

    def deliver_signal(
        self,
        *,
        owner_user_id: int,
        workflow_id: int,
        signal_key: str,
        payload: Mapping[str, FrozenJSONValue],
        expected_revision: int,
        now: datetime,
    ) -> WorkflowSignalOutcome:
        digest = hashlib.sha256(canonical_json_text(_thaw(payload)).encode("utf-8")).hexdigest()
        text = canonical_json_text(_thaw(payload))

        def operation(connection: Connection) -> WorkflowSignalOutcome:
            workflow = _owned_execution(connection, owner_user_id, workflow_id)
            prior = (
                connection.execute(
                    select(WorkflowSignalRecord).where(
                        WorkflowSignalRecord.workflow_id == workflow_id,
                        WorkflowSignalRecord.signal_key == signal_key,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if prior is not None:
                if str(prior["payload_digest"]) != digest:
                    raise WorkflowConflict("signal identity already used with different content")
                # An identical replay returns the prior durable outcome and does not advance
                # the workflow a second time.
                return WorkflowSignalOutcome(
                    accepted=str(prior["outcome"]) == SIGNAL_ACCEPTED,
                    outcome=(
                        SIGNAL_REPLAYED
                        if str(prior["outcome"]) == SIGNAL_ACCEPTED
                        else str(prior["outcome"])
                    ),
                    workflow=workflow,
                )
            if workflow.status is WorkflowStatus.CANCELLED:
                outcome = SIGNAL_CANCELLED
            elif expected_revision != workflow.checkpoint_revision:
                outcome = SIGNAL_STALE_REVISION
            elif (
                workflow.status is not WorkflowStatus.WAITING
                or workflow.wait_kind is not WorkflowWaitKind.SIGNAL
                or workflow.signal_key != signal_key
            ):
                outcome = SIGNAL_NOT_WAITING
            else:
                outcome = SIGNAL_ACCEPTED
            accepted = outcome == SIGNAL_ACCEPTED
            if not accepted:
                # A refused delivery deliberately does **not** occupy the identity key. Only an
                # accepted delivery reserves it, so a stale or wrong-key probe cannot burn the
                # name and block the legitimate delivery that follows. The refusal itself is
                # returned to the owner synchronously and the workflow's durable state already
                # shows it was not waiting.
                return WorkflowSignalOutcome(accepted=False, outcome=outcome, workflow=workflow)
            connection.execute(
                insert(WorkflowSignalRecord).values(
                    workflow_id=workflow_id,
                    owner_user_id=owner_user_id,
                    signal_key=signal_key,
                    payload_digest=digest,
                    signal_json=text,
                    expected_revision=expected_revision,
                    outcome=outcome,
                    received_at=now,
                    accepted_at=now,
                )
            )
            connection.execute(
                update(WorkflowExecutionRecord)
                .where(WorkflowExecutionRecord.id == workflow_id)
                .values(
                    status=WorkflowStatus.RUNNABLE.value,
                    wait_kind=None,
                    wakeup_at=None,
                    signal_key=None,
                    updated_at=now,
                )
            )
            return WorkflowSignalOutcome(
                accepted=True, outcome=outcome, workflow=_reload_execution(connection, workflow_id)
            )

        return self._runner.run(operation)

    def list_decisions(
        self, *, owner_user_id: int, workflow_id: int
    ) -> tuple[WorkflowDecisionView, ...]:
        def operation(connection: Connection) -> tuple[WorkflowDecisionView, ...]:
            _owned_execution(connection, owner_user_id, workflow_id)
            return tuple(
                _decision_from_row(row)
                for row in connection.execute(
                    select(WorkflowDecisionRecord)
                    .where(
                        WorkflowDecisionRecord.workflow_id == workflow_id,
                        WorkflowDecisionRecord.owner_user_id == owner_user_id,
                    )
                    .order_by(WorkflowDecisionRecord.id.asc())
                )
                .mappings()
                .all()
            )

        return self._runner.run(operation)

    def decide(
        self,
        *,
        owner_user_id: int,
        workflow_id: int,
        decision_id: int,
        approve: bool,
        expected_revision: int,
        now: datetime,
    ) -> WorkflowDecisionView:
        def operation(connection: Connection) -> WorkflowDecisionView:
            _owned_execution(connection, owner_user_id, workflow_id)
            row = (
                connection.execute(
                    select(WorkflowDecisionRecord).where(
                        WorkflowDecisionRecord.id == decision_id,
                        WorkflowDecisionRecord.workflow_id == workflow_id,
                        WorkflowDecisionRecord.owner_user_id == owner_user_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise WorkflowNotFound
            current = WorkflowDecisionState(str(row["state"]))
            if current not in DECIDABLE_DECISION_STATES:
                raise WorkflowConflict("decision is no longer pending")
            if int(row["checkpoint_revision"]) != expected_revision:
                # Editing or racing the revision the decision was bound to invalidates it.
                raise WorkflowConflict("decision is bound to a different checkpoint revision")
            if workflow_identity_moved(connection, workflow_id, row):
                raise WorkflowConflict("pinned package or configuration identity changed")
            if row["expires_at"] is not None and row["expires_at"] <= now:
                connection.execute(
                    update(WorkflowDecisionRecord)
                    .where(WorkflowDecisionRecord.id == decision_id)
                    .values(
                        state=WorkflowDecisionState.EXPIRED.value,
                        decided_at=now,
                    )
                )
                raise WorkflowConflict("decision expired")
            target = WorkflowDecisionState.APPROVED if approve else WorkflowDecisionState.DENIED
            updated = connection.execute(
                update(WorkflowDecisionRecord)
                .where(
                    WorkflowDecisionRecord.id == decision_id,
                    WorkflowDecisionRecord.state == WorkflowDecisionState.PENDING.value,
                )
                .values(state=target.value, decided_at=now)
            )
            if updated.rowcount != 1:
                raise WorkflowConflict("decision is no longer pending")
            # An approved decision releases the wait it was proposed for. It still grants
            # nothing: the next step re-checks every live authority at dispatch.
            connection.execute(
                update(WorkflowExecutionRecord)
                .where(
                    WorkflowExecutionRecord.id == workflow_id,
                    WorkflowExecutionRecord.status == WorkflowStatus.WAITING.value,
                    WorkflowExecutionRecord.wait_kind == WorkflowWaitKind.OWNER_DECISION.value,
                    WorkflowExecutionRecord.checkpoint_revision == expected_revision + 1,
                )
                .values(
                    status=WorkflowStatus.RUNNABLE.value
                    if approve
                    else WorkflowStatus.CANCELLED.value,
                    wait_kind=None,
                    wakeup_at=None,
                    decision_key=None,
                    finished_at=None if approve else now,
                    updated_at=now,
                )
            )
            refreshed = (
                connection.execute(
                    select(WorkflowDecisionRecord).where(WorkflowDecisionRecord.id == decision_id)
                )
                .mappings()
                .one()
            )
            return _decision_from_row(refreshed)

        return self._runner.run(operation)


def workflow_identity_moved(connection: Connection, workflow_id: int, decision: RowMapping) -> bool:
    """Whether the pinned package/config identity behind a decision has since changed."""
    row = (
        connection.execute(
            select(
                WorkflowExecutionRecord.package_content_digest,
                WorkflowExecutionRecord.effective_config_digest,
            ).where(WorkflowExecutionRecord.id == workflow_id)
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return True
    return (
        row["package_content_digest"] != decision["package_content_digest"]
        or row["effective_config_digest"] != decision["effective_config_digest"]
    )


def _owned_execution(
    connection: Connection, owner_user_id: int, workflow_id: int
) -> WorkflowExecution:
    row = (
        connection.execute(
            select(WorkflowExecutionRecord).where(
                WorkflowExecutionRecord.id == workflow_id,
                WorkflowExecutionRecord.owner_user_id == owner_user_id,
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        # Foreign and nonexistent are indistinguishable, so this surface cannot probe for
        # another owner's workflow.
        raise WorkflowNotFound
    return _execution_from_row(row)


def _replay_or_conflict(
    connection: Connection, request: WorkflowCreationRequest
) -> WorkflowExecution | None:
    row = (
        connection.execute(
            select(WorkflowExecutionRecord).where(
                WorkflowExecutionRecord.owner_user_id == request.owner_user_id,
                WorkflowExecutionRecord.submission_key == request.submission_key,
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    if str(row["submission_digest"]) != content_digest(
        {
            "workflow_kind": request.workflow_kind,
            "agent_instance_id": request.agent_instance_id,
            "input_text": request.input_text,
            "max_steps": request.budget.max_steps,
            "model_calls": request.budget.model_call_reservation,
            "tool_calls": request.budget.tool_call_reservation,
            "output_tokens": request.budget.output_token_reservation,
            "deadline_hours": request.budget.deadline_hours,
        }
    ):
        raise WorkflowConflict("submission identity already exists with different content")
    return _execution_from_row(row)


# ---------------------------------------------------------------------------------------
# Reading the step a Run resumes from
# ---------------------------------------------------------------------------------------


class SqlAlchemyWorkflowStepSource:
    """Read-only lookup of the checkpoint one dispatched Run must resume from.

    Separate from :class:`SqlAlchemyWorkflowStepFinalizer` on purpose. This half has to
    answer "is this Run a workflow step?" *before* the Worker decides whether to hand a
    package anything at all, so it must not write, fence, or reserve. Keeping the read and
    the commit in different objects also means the commit half cannot be reused to read
    someone else's state.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def snapshot_for_run(self, *, run_id: int) -> WorkflowStepSnapshot | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(
                        WorkflowStepRecord.step_number,
                        WorkflowStepRecord.workflow_id,
                        WorkflowStepRecord.expected_checkpoint_revision,
                        WorkflowStepRecord.state_json,
                        WorkflowStepRecord.state_schema_version,
                    ).where(
                        WorkflowStepRecord.run_id == run_id,
                        WorkflowStepRecord.status == "running",
                    )
                )
                .mappings()
                .one_or_none()
            )
            wake_signal: FrozenState = freeze_state({})
            if row is not None:
                signal = (
                    connection.execute(
                        select(WorkflowSignalRecord.signal_key, WorkflowSignalRecord.signal_json)
                        .where(
                            WorkflowSignalRecord.workflow_id == row["workflow_id"],
                            WorkflowSignalRecord.expected_revision
                            == row["expected_checkpoint_revision"],
                            WorkflowSignalRecord.outcome == SIGNAL_ACCEPTED,
                        )
                        .order_by(WorkflowSignalRecord.id.desc())
                        .limit(1)
                    )
                    .mappings()
                    .one_or_none()
                )
                if signal is not None:
                    wake_signal = freeze_state(
                        {
                            "key": str(signal["signal_key"]),
                            "payload": require_json_value(_loads(signal["signal_json"])),
                        }
                    )
        if row is None:
            return None
        return WorkflowStepSnapshot(
            step_number=int(row["step_number"]),
            checkpoint_revision=int(row["expected_checkpoint_revision"]),
            state_version=int(row["state_schema_version"]),
            state=freeze_state(require_json_value(_loads(str(row["state_json"])))),
            wake_signal=wake_signal,
        )


# ---------------------------------------------------------------------------------------
# The fenced Worker finalization boundary
# ---------------------------------------------------------------------------------------


class SqlAlchemyWorkflowStepFinalizer:
    """Commit one step's checkpoint on the caller's *own* fenced connection.

    This is the only writable workflow path the Worker has. It is handed the connection
    `succeed` is already inside, so Attempt/Job/Run success, memory-proposal capture, the
    checkpoint revision, the step outcome, and the workflow's own status all commit or roll
    back together. There is no second writer and no post-hoc repair path.

    A stale Attempt cannot checkpoint: the step is selected by `run_id` *and* fenced on the
    workflow's current checkpoint revision *and* on the step being `running`, all on this
    connection. A lost fence returns False and the enclosing `succeed` rolls the whole
    terminalization back rather than committing a Run that has no checkpoint.
    """

    def __init__(self, *, retain_checkpoints: int | None = None) -> None:
        self._retain_checkpoints = retain_checkpoints

    def commit_step(
        self,
        connection: object,
        *,
        run_id: int,
        attempt_id: int,
        result: WorkflowStepResult,
        now: datetime,
    ) -> bool:
        conn = cast(Connection, connection)
        step_row = (
            conn.execute(select(WorkflowStepRecord).where(WorkflowStepRecord.run_id == run_id))
            .mappings()
            .one_or_none()
        )
        if step_row is None or int(step_row["workflow_id"]) <= 0:
            # An ordinary Run, or a workflow Run whose step was never linked. Refusing here
            # is what keeps a non-workflow Run byte-for-byte unchanged.
            raise WorkflowNotLinked("run has no workflow step")
        workflow_id = int(step_row["workflow_id"])
        workflow_row = (
            conn.execute(
                select(WorkflowExecutionRecord).where(WorkflowExecutionRecord.id == workflow_id)
            )
            .mappings()
            .one_or_none()
        )
        if workflow_row is None:
            raise WorkflowNotLinked("workflow no longer exists")
        if str(workflow_row["status"]) == WorkflowStatus.CANCELLED.value:
            # Parent cancellation forbids future steps but does not cancel its active Run.
            # Permit ordinary fenced Run completion without resurrecting the workflow.
            conn.execute(
                update(WorkflowStepRecord)
                .where(WorkflowStepRecord.id == int(step_row["id"]))
                .values(status=WorkflowStepStatus.SUCCEEDED.value, finished_at=now)
            )
            return True
        if (
            str(step_row["status"]) != WorkflowStepStatus.RUNNING.value
            or int(step_row["expected_checkpoint_revision"])
            != int(workflow_row["checkpoint_revision"])
            or str(workflow_row["status"]) != WorkflowStatus.RUNNING.value
        ):
            # Stale Attempt, already-advanced revision, or a workflow that moved on: no
            # checkpoint, no progress, and nothing partially applied.
            return False

        directive = result.directive
        state = result.state
        revision = int(workflow_row["checkpoint_revision"]) + 1
        conn.execute(
            insert(WorkflowCheckpointRecord).values(
                workflow_id=workflow_id,
                revision=revision,
                step_number=int(step_row["step_number"]),
                state_schema_version=WORKFLOW_STATE_SCHEMA_VERSION,
                state_json=_frozen_to_json(state),
                state_digest=state_digest(state),
                created_at=now,
            )
        )
        conn.execute(
            update(WorkflowStepRecord)
            .where(
                WorkflowStepRecord.id == int(step_row["id"]),
                WorkflowStepRecord.status == WorkflowStepStatus.RUNNING.value,
            )
            .values(
                status=WorkflowStepStatus.SUCCEEDED.value,
                summary=result.summary,
                finished_at=now,
            )
        )
        if result.decision_request is not None:
            _insert_decision_on_connection(conn, workflow_id, result, now)

        if directive.completes:
            target_status, wait_kind, wakeup_at, signal_key, decision_key = (
                WorkflowStatus.SUCCEEDED,
                None,
                None,
                None,
                None,
            )
        elif directive.waits:
            target_status = WorkflowStatus.WAITING
            wait_kind = directive.wait_kind
            wakeup_at = resolved_wakeup_at(directive, now)
            signal_key = directive.signal_key
            decision_key = directive.decision_key
        else:
            target_status, wait_kind, wakeup_at, signal_key, decision_key = (
                WorkflowStatus.RUNNABLE,
                None,
                None,
                None,
                None,
            )
        finished_at = now if target_status in TERMINAL_WORKFLOW_STATUSES else None
        conn.execute(
            update(WorkflowExecutionRecord)
            .where(
                WorkflowExecutionRecord.id == workflow_id,
                WorkflowExecutionRecord.checkpoint_revision
                == int(workflow_row["checkpoint_revision"]),
                WorkflowExecutionRecord.status == WorkflowStatus.RUNNING.value,
            )
            .values(
                status=target_status.value,
                checkpoint_revision=revision,
                wait_kind=wait_kind.value if wait_kind else None,
                wakeup_at=wakeup_at,
                signal_key=signal_key,
                decision_key=decision_key,
                updated_at=now,
                finished_at=finished_at,
            )
        )
        _record_reported_usage(conn, workflow_id, now)
        retained = int(workflow_row["max_retained_checkpoints"])
        conn.execute(
            delete(WorkflowCheckpointRecord).where(
                WorkflowCheckpointRecord.workflow_id == workflow_id,
                WorkflowCheckpointRecord.revision <= revision - retained,
            )
        )
        return True


def _insert_decision_on_connection(
    connection: Connection, workflow_id: int, result: WorkflowStepResult, now: datetime
) -> None:
    request = result.decision_request
    assert request is not None
    workflow_row = (
        connection.execute(
            select(
                WorkflowExecutionRecord.owner_user_id,
                WorkflowExecutionRecord.checkpoint_revision,
            ).where(WorkflowExecutionRecord.id == workflow_id)
        )
        .mappings()
        .one()
    )
    expires_at = request.expires_at
    if expires_at <= now:
        # An already-expired proposal is recorded as expired rather than pending, so it can
        # never be decided later against a stale moment.
        expires_at = now + timedelta(seconds=1)
    connection.execute(
        insert(WorkflowDecisionRecord).values(
            workflow_id=workflow_id,
            owner_user_id=int(workflow_row["owner_user_id"]),
            checkpoint_revision=request.checkpoint_revision,
            tool_definition_id=request.tool_definition_id,
            upstream_name=request.upstream_name,
            action_fingerprint=request.action_fingerprint,
            arguments_digest=decision_arguments_digest(request.arguments),
            preview_json=_frozen_to_json(request.preview),
            package_content_digest=request.package_content_digest,
            effective_config_digest=request.effective_config_digest,
            state=WorkflowDecisionState.PENDING.value,
            requested_at=now,
            expires_at=expires_at,
        )
    )


def _record_reported_usage(connection: Connection, workflow_id: int, now: datetime) -> None:
    """Touch `updated_at` after folding reported provider usage into the workflow.

    Reported token usage is *observational*: it is recorded for the owner's view and never
    treated as the reservation, which is what ADR 0039 forbids claiming as a hard input-token
    budget.
    """
    connection.execute(
        update(WorkflowExecutionRecord)
        .where(WorkflowExecutionRecord.id == workflow_id)
        .values(updated_at=now)
    )


# ---------------------------------------------------------------------------------------
# The Scheduler tick
# ---------------------------------------------------------------------------------------


class SqlAlchemyWorkflowContinuationPersistence:
    """The bounded, fair continuation tick.

    It scans a bounded page of due workflows ordered by the durable `ix_workflow_executions_due`
    index, skips paused/terminal/deadline/blocked ones without consuming a dispatch, and
    submits at most `dispatch_limit` continuations. A workflow that cannot be dispatched is
    left where it is rather than retried first on the next tick, so one blocked workflow
    cannot hide every later due one.
    """

    def __init__(
        self,
        engine: Engine,
        limits_resolver: WorkflowLimitsResolver,
        *,
        limits: tuple[int, int, int] = (1000, 1000, 1000),
        scan_limit: int = WORKFLOW_TICK_SCAN_LIMIT,
        dispatch_limit: int = WORKFLOW_TICK_DISPATCH_LIMIT,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._engine = engine
        self._limits_resolver = limits_resolver
        self._runner = TransactionRunner(engine, sleep)
        self._capacity, self._agent_capacity, self._provider_capacity = limits
        self._scan_limit = scan_limit
        self._dispatch_limit = dispatch_limit

    def tick(self, *, now: datetime) -> WorkflowTickReport:
        counts: dict[str, int] = dict.fromkeys(
            (
                "examined",
                "dispatched",
                "deferred_capacity",
                "deferred_config",
                "needs_review",
                "failed",
            ),
            0,
        )
        for _ in range(self._dispatch_limit):
            if self._runner.run(lambda connection: self._one(connection, now, counts)):
                continue
            break
        return WorkflowTickReport(**counts)

    def _one(self, connection: Connection, now: datetime, counts: dict[str, int]) -> bool:
        """Handle exactly one due workflow. Returns True when anything changed."""
        terminal_run = (
            select(WorkflowStepRecord.id)
            .join(RunRecord, RunRecord.id == WorkflowStepRecord.run_id)
            .where(
                WorkflowStepRecord.workflow_id == WorkflowExecutionRecord.id,
                WorkflowStepRecord.status == WorkflowStepStatus.RUNNING.value,
                RunRecord.status.in_(
                    (RunStatus.FAILED.value, RunStatus.CANCELLED.value, RunStatus.SUCCEEDED.value)
                ),
            )
            .exists()
        )
        due = (
            connection.execute(
                select(WorkflowExecutionRecord.id)
                .where(
                    (
                        terminal_run
                        & (WorkflowExecutionRecord.status == WorkflowStatus.RUNNING.value)
                    )
                    | (
                        WorkflowExecutionRecord.paused.is_(False)
                        & WorkflowExecutionRecord.status.in_(
                            (
                                WorkflowStatus.PENDING.value,
                                WorkflowStatus.RUNNABLE.value,
                                WorkflowStatus.WAITING.value,
                            )
                        )
                        & (
                            (WorkflowExecutionRecord.deadline_at <= now)
                            | (
                                (
                                    WorkflowExecutionRecord.wait_kind.is_(None)
                                    | (
                                        WorkflowExecutionRecord.wait_kind
                                        == WorkflowWaitKind.TIME.value
                                    )
                                )
                                & (
                                    WorkflowExecutionRecord.wakeup_at.is_(None)
                                    | (WorkflowExecutionRecord.wakeup_at <= now)
                                )
                            )
                        )
                    ),
                )
                .order_by(
                    WorkflowExecutionRecord.updated_at.asc(),
                    WorkflowExecutionRecord.id.asc(),
                )
                .limit(self._scan_limit)
            )
            .scalars()
            .all()
        )
        counts["examined"] += len(due)
        for workflow_id in due:
            changed = self._advance(connection, int(workflow_id), now, counts)
            if changed:
                return True
            # Rotate admission-blocked work across ticks without changing its authority.
            connection.execute(
                update(WorkflowExecutionRecord)
                .where(WorkflowExecutionRecord.id == workflow_id)
                .values(updated_at=now)
            )
        return False

    def _advance(
        self, connection: Connection, workflow_id: int, now: datetime, counts: dict[str, int]
    ) -> bool:
        workflow = _reload_execution(connection, workflow_id)
        if workflow.status is WorkflowStatus.RUNNING:
            # All terminal paths (including recovery/cancel) converge here. Never replay
            # or synthesize a checkpoint after an uncertain effect.
            connection.execute(
                update(WorkflowStepRecord)
                .where(
                    WorkflowStepRecord.workflow_id == workflow_id,
                    WorkflowStepRecord.status == WorkflowStepStatus.RUNNING.value,
                )
                .values(status=WorkflowStepStatus.FAILED.value, finished_at=now)
            )
            _finish(connection, workflow, WorkflowStatus.NEEDS_REVIEW, REVIEW_RUN_TERMINAL, now)
            counts["needs_review"] += 1
            return True
        if now >= workflow.deadline_at:
            _finish(connection, workflow, WorkflowStatus.FAILED, REVIEW_DEADLINE_EXCEEDED, now)
            counts["failed"] += 1
            return True
        if workflow.wait_kind is WorkflowWaitKind.SIGNAL or (
            workflow.wait_kind is WorkflowWaitKind.OWNER_DECISION
        ):
            # Only an owner operation may release these waits. A tick never invents the
            # signal or the decision that a step asked for.
            return False
        if workflow.step_count >= workflow.budget.max_steps:
            _finish(connection, workflow, WorkflowStatus.FAILED, REVIEW_STEP_LIMIT, now)
            counts["failed"] += 1
            return True

        approved = (
            connection.execute(
                select(WorkflowDecisionRecord).where(
                    WorkflowDecisionRecord.workflow_id == workflow_id,
                    WorkflowDecisionRecord.checkpoint_revision == workflow.checkpoint_revision - 1,
                    WorkflowDecisionRecord.state == WorkflowDecisionState.APPROVED.value,
                    WorkflowDecisionRecord.consumed_at.is_(None),
                )
            )
            .mappings()
            .one_or_none()
        )
        if approved is not None and approved["expires_at"] <= now:
            connection.execute(
                update(WorkflowDecisionRecord)
                .where(WorkflowDecisionRecord.id == approved["id"])
                .values(state=WorkflowDecisionState.EXPIRED.value)
            )
            _finish(connection, workflow, WorkflowStatus.NEEDS_REVIEW, REVIEW_AUTHORITY_LOST, now)
            counts["needs_review"] += 1
            return True

        latest = (
            connection.execute(
                select(WorkflowCheckpointRecord.state_json, WorkflowCheckpointRecord.state_digest)
                .where(WorkflowCheckpointRecord.workflow_id == workflow_id)
                .order_by(WorkflowCheckpointRecord.revision.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        checkpoint_state = (
            freeze_state(require_json_value(_loads(latest["state_json"])))
            if latest
            else freeze_state({})
        )
        if not _instance_eligible(connection, workflow):
            # A disabled or reassigned Agent Instance is an ordinary owner action, not a
            # configuration drift: the workflow stays due and re-enabling it resumes work.
            counts["deferred_config"] += 1
            return False
        limits = _resolve_limits(connection, workflow, self._limits_resolver)
        if limits is None:
            _finish(
                connection,
                workflow,
                WorkflowStatus.NEEDS_REVIEW,
                REVIEW_CONFIGURATION_CHANGED,
                now,
            )
            counts["needs_review"] += 1
            return True
        try:
            submit_continuation_on_connection(
                connection,
                workflow=workflow,
                step_number=workflow.step_count + 1,
                checkpoint_state=checkpoint_state,
                expected_revision=workflow.checkpoint_revision,
                limits=limits,
                now=now,
                capacity=self._capacity,
                agent_capacity=self._agent_capacity,
                provider_capacity=self._provider_capacity,
            )
        except _Uncertain as error:
            return self._defer(connection, workflow, now, counts, str(error))
        except DurableSubmissionRejected:
            # A disabled instance, a foreign owner, or a mismatched definition: ordinary
            # durable rejection. The workflow stays due and is reported rather than failed,
            # because an owner re-enabling the instance is a normal thing to do.
            counts["deferred_config"] += 1
            return False
        except QueueCapacityExceeded:
            counts["deferred_capacity"] += 1
            return False
        counts["dispatched"] += 1
        if approved is not None:
            connection.execute(
                update(WorkflowDecisionRecord)
                .where(
                    WorkflowDecisionRecord.id == approved["id"],
                    WorkflowDecisionRecord.consumed_at.is_(None),
                )
                .values(consumed_at=now, state=WorkflowDecisionState.CONSUMED.value)
            )
        return True

    def _defer(
        self,
        connection: Connection,
        workflow: WorkflowExecution,
        now: datetime,
        counts: dict[str, int],
        reason: str,
    ) -> bool:
        if "identity" in reason:
            _finish(
                connection, workflow, WorkflowStatus.NEEDS_REVIEW, REVIEW_CONFIGURATION_CHANGED, now
            )
            counts["needs_review"] += 1
            return True
        if "reservation" in reason or "step budget" in reason:
            _finish(connection, workflow, WorkflowStatus.FAILED, REVIEW_BUDGET_EXHAUSTED, now)
            counts["failed"] += 1
            return True
        # A moved revision means another writer already advanced this workflow; nothing to do
        # here and nothing is counted as a deferral, because the work was not skipped.
        return False


def _finish(
    connection: Connection,
    workflow: WorkflowExecution,
    status: WorkflowStatus,
    reason: str,
    now: datetime,
) -> None:
    if reason not in INVALID_REVIEW_REASONS:
        raise WorkflowConflict("workflow terminal reason is outside the closed set")
    connection.execute(
        update(WorkflowExecutionRecord)
        .where(
            WorkflowExecutionRecord.id == workflow.id,
            WorkflowExecutionRecord.status.in_(_ACTIVE_STATUSES),
        )
        .values(
            status=status.value,
            wait_kind=None,
            wakeup_at=None,
            signal_key=None,
            decision_key=None,
            review_reason=reason,
            updated_at=now,
            finished_at=now,
        )
    )


def _instance_eligible(connection: Connection, workflow: WorkflowExecution) -> bool:
    """Whether the pinned Agent Instance is still the owner's, enabled, and the same definition.

    Read on the transaction that inserts the Run, so a disable committed a moment earlier is
    ordered *before* this submission rather than racing it.
    """
    instance = (
        connection.execute(
            select(AgentInstanceRecord).where(
                AgentInstanceRecord.id == workflow.agent_instance_id,
                AgentInstanceRecord.owner_user_id == workflow.owner_user_id,
                AgentInstanceRecord.enabled.is_(True),
            )
        )
        .mappings()
        .one_or_none()
    )
    if instance is None:
        return False
    return (
        str(instance["agent_key"]) == workflow.agent_key
        and str(instance["agent_definition_version"]) == workflow.agent_definition_version
    )


def _resolve_limits(
    connection: Connection,
    workflow: WorkflowExecution,
    resolver: WorkflowLimitsResolver,
) -> RunLimits | None:
    """Resolve the current per-step Run limits, or None when the definition no longer resolves.

    A definition that cannot be resolved is a configuration change, which is `needs_review` --
    never an invented default that would run the remaining steps under limits nobody approved.
    """
    del connection
    try:
        limits = resolver(AgentDefinitionId(workflow.agent_key, workflow.agent_definition_version))
    except Exception:
        return None
    return limits


__all__ = [
    "SqlAlchemyWorkflowContinuationPersistence",
    "SqlAlchemyWorkflowPersistence",
    "SqlAlchemyWorkflowStepFinalizer",
    "create_workflow_with_step_on_connection",
    "submit_continuation_on_connection",
    "workflow_identity_moved",
]
