"""W1-W3 durable workflow application services (ADR 0039).

Three services live here, and each owns exactly one boundary:

* :class:`WorkflowService` -- the **control plane**. Owners create a workflow, read its
  progress, send a wakeup signal, decide a proposed action, and pause/resume/cancel.
  It never runs a model, a tool, or a package.
* :class:`WorkflowContinuationService` -- the **Scheduler's** bounded tick. It is the only
  thing that may create a workflow continuation, and it does so through the canonical
  Run/Job insertion primitive so existing admission, fairness and concurrency rules apply
  unchanged.
* :class:`WorkflowStepCommitter` -- the narrow protocol the fenced Worker finalization
  boundary calls on its *own* connection, so a step's Run success and its checkpoint commit
  as one transaction or not at all.

None of these holds a database session. Persistence is injected, so the same services are
usable from the API process, the Scheduler process, and tests against a temporary database.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Protocol

from nervos_core.application.clock import Clock, require_utc
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.runs import RunLimits
from nervos_core.domain.tools import JsonValue, canonical_json_text
from nervos_core.domain.workflows import (
    DEADLINE_HOURS_HARD,
    INVALID_REVIEW_REASONS,
    REVIEW_AUTHORITY_LOST,
    REVIEW_RUN_TERMINAL,
    TERMINAL_WORKFLOW_STATUSES,
    WORKFLOW_JOB_MAX_ATTEMPTS,
    FrozenJSONValue,
    WorkflowBudget,
    WorkflowCheckpoint,
    WorkflowDecisionRequest,
    WorkflowDecisionState,
    WorkflowDirective,
    WorkflowExecution,
    WorkflowNotFound,
    WorkflowReservation,
    WorkflowReservations,
    WorkflowStatus,
    WorkflowStep,
    WorkflowStepResult,
    WorkflowTransitionError,
    WorkflowWaitKind,
    accepts_reservation,
    active_workflow_count_limit,
    assert_transition,
    freeze_signal,
    freeze_state,
    validate_safe_key,
)

# Bounded keyset page. A workflow list is an unbounded table over time, so both the read
# path and the API page are bounded rather than "limit the SQL and hope".
WORKFLOW_PAGE_LIMIT_MAX = 100
WORKFLOW_STEP_PAGE_LIMIT_MAX = 200
#: How many workflows one bounded tick examines. A workflow that is merely blocked must not
#: be able to hide every later due workflow, so the tick pages in fair, bounded rounds
#: rather than scanning until it finds something it can dispatch.
WORKFLOW_TICK_SCAN_LIMIT = 200
#: How many continuations one tick may submit. Bounded so a backlog cannot turn one tick
#: into an unbounded burst of submissions.
WORKFLOW_TICK_DISPATCH_LIMIT = 20


class WorkflowCapacityExceeded(ValueError):
    """The owner already has the maximum number of active workflows."""


@dataclass(frozen=True, slots=True)
class WorkflowStepView:
    """One step as the owner sees it: identity, pinned revision, and linked Run."""

    step: WorkflowStep
    run_id: int | None
    run_status: str | None
    job_phase: str | None


@dataclass(frozen=True, slots=True)
class WorkflowDetail:
    """Everything one owner-inspected workflow exposes, and nothing public about it.

    Checkpoint *content* is deliberately absent from the list projection and present here
    only, because full application state is owner inspection rather than a public status or
    audit projection.
    """

    workflow: WorkflowExecution
    steps: tuple[WorkflowStepView, ...]
    checkpoints: tuple[WorkflowCheckpoint, ...]
    decisions: tuple[WorkflowDecisionView, ...]
    signals: tuple[WorkflowSignalView, ...]


@dataclass(frozen=True, slots=True)
class WorkflowDecisionView:
    id: int
    checkpoint_revision: int
    tool_definition_id: int
    upstream_name: str
    arguments_digest: str
    preview: Mapping[str, FrozenJSONValue]
    state: WorkflowDecisionState
    requested_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    consumed_at: datetime | None


@dataclass(frozen=True, slots=True)
class WorkflowSignalView:
    id: int
    signal_key: str
    expected_revision: int
    outcome: str
    received_at: datetime
    accepted_at: datetime | None


@dataclass(frozen=True, slots=True)
class WorkflowSignalOutcome:
    """The durable result of one signal delivery, including an identical replay.

    `accepted` is False for a duplicate, a stale revision, or a workflow that is not
    waiting for this key; in every one of those cases the workflow's state is unchanged and
    `outcome` says why.
    """

    accepted: bool
    outcome: str
    workflow: WorkflowExecution


SIGNAL_ACCEPTED = "accepted"
SIGNAL_REPLAYED = "replayed"
SIGNAL_STALE_REVISION = "stale_revision"
SIGNAL_NOT_WAITING = "not_waiting"
SIGNAL_PAYLOAD_CONFLICT = "payload_conflict"
SIGNAL_CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class WorkflowTickReport:
    """Bounded counters for one Scheduler workflow tick.

    Deliberately no `deferred_paused`: the due-work index leads with `paused`, so a paused
    workflow is never examined rather than examined and refused. A counter that is
    structurally always zero is a field that will one day be read as a real signal.
    """

    examined: int
    dispatched: int
    deferred_capacity: int
    deferred_config: int
    needs_review: int
    failed: int


# ---------------------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------------------


class WorkflowPersistence(Protocol):
    """Owner-scoped durable workflow operations. Implemented only in infrastructure."""

    def create_workflow(self, request: WorkflowCreationRequest) -> WorkflowExecution: ...

    def get_workflow(self, *, owner_user_id: int, workflow_id: int) -> WorkflowExecution: ...

    def list_workflows(
        self, *, owner_user_id: int, limit: int, before_id: int | None
    ) -> tuple[WorkflowExecution, ...]: ...

    def count_active(self, *, owner_user_id: int) -> int: ...

    def detail(self, *, owner_user_id: int, workflow_id: int) -> WorkflowDetail: ...

    def set_paused(
        self, *, owner_user_id: int, workflow_id: int, paused: bool, now: datetime
    ) -> WorkflowExecution: ...

    def cancel(
        self, *, owner_user_id: int, workflow_id: int, now: datetime
    ) -> WorkflowExecution: ...

    def deliver_signal(
        self,
        *,
        owner_user_id: int,
        workflow_id: int,
        signal_key: str,
        payload: Mapping[str, FrozenJSONValue],
        expected_revision: int,
        now: datetime,
    ) -> WorkflowSignalOutcome: ...

    def list_decisions(
        self, *, owner_user_id: int, workflow_id: int
    ) -> tuple[WorkflowDecisionView, ...]: ...

    def decide(
        self,
        *,
        owner_user_id: int,
        workflow_id: int,
        decision_id: int,
        approve: bool,
        expected_revision: int,
        now: datetime,
    ) -> WorkflowDecisionView: ...


class WorkflowContinuationPersistence(Protocol):
    """The Scheduler-only durable continuation port."""

    def tick(self, *, now: datetime) -> WorkflowTickReport: ...


class WorkflowStepCommitter(Protocol):
    """Commit one step's checkpoint inside the caller's fenced finalization transaction.

    Declared structurally rather than against a SQLAlchemy `Connection` so the fenced Job
    persistence can accept it without importing any workflow infrastructure, and so a test
    can supply a fake that records the connection it was handed.
    """

    def commit_step(
        self,
        connection: object,
        *,
        run_id: int,
        attempt_id: int,
        result: WorkflowStepResult,
        now: datetime,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class WorkflowStepSnapshot:
    """The exact checkpoint one dispatched step resumes from.

    Read-only and already frozen. It carries no lease, claim or owner authority: handing it
    to a package grants that package the state it already owns and nothing else.
    """

    step_number: int
    checkpoint_revision: int
    state_version: int
    state: Mapping[str, FrozenJSONValue]
    wake_signal: Mapping[str, FrozenJSONValue] = field(default_factory=lambda: freeze_state({}))

    def __post_init__(self) -> None:
        # Frozen here rather than trusted, so the declared type is true for every caller
        # and not only for the SQLAlchemy source that happens to normalize on the way in.
        object.__setattr__(self, "state", freeze_state(dict(self.state)))
        object.__setattr__(self, "wake_signal", freeze_state(dict(self.wake_signal)))


class WorkflowStepSnapshotSource(Protocol):
    """Read the checkpoint a Run must resume from. Returns ``None`` for an ordinary Run.

    Deliberately a *read*: the Worker needs to know whether a Run is a workflow step before
    it hands anything to package code, and it must be able to answer "no" without writing,
    starting a transaction that fences, or reserving anything.
    """

    def snapshot_for_run(self, *, run_id: int) -> WorkflowStepSnapshot | None: ...


@dataclass(frozen=True, slots=True)
class WorkflowCreationRequest:
    """Everything one workflow creation binds, decided once, before any write."""

    owner_user_id: int
    agent_instance_id: int
    workflow_kind: str
    submission_key: str
    input_text: str
    limits: RunLimits
    definition_id: AgentDefinitionId | None
    budget: WorkflowBudget
    now: datetime
    max_active: int = 16
    capacity: int = 1000
    agent_capacity: int = 1000
    provider_capacity: int = 1000


# ---------------------------------------------------------------------------------------
# Control plane
# ---------------------------------------------------------------------------------------


class WorkflowService:
    """Owner-scoped workflow control plane: create, inspect, signal, decide, and lifecycle.

    Every method takes the authenticated owner id from the server. No method accepts an
    owner id, a workflow id, or a revision from the request body as authority, and a
    foreign workflow is indistinguishable from a nonexistent one.
    """

    def __init__(
        self,
        persistence: WorkflowPersistence,
        *,
        clock: Clock,
        max_active: int = 16,
    ) -> None:
        self._persistence = persistence
        self._clock = clock
        self._max_active = active_workflow_count_limit(max_active)

    def create(self, request: WorkflowCreationRequest) -> WorkflowExecution:
        """Durably accept one workflow and its first step, or replay the identical request.

        Identity is `(owner, submission_key)` and content is a canonical digest, so a
        retried creation after an uncertain commit returns the original workflow, while a
        different body under the same key is a conflict rather than a second workflow.

        The configured active-workflow ceiling is applied here rather than trusted from the
        caller, so a caller cannot raise its own limit by passing a bigger number.
        """
        bounded = (
            request
            if request.max_active <= self._max_active
            else replace(request, max_active=self._max_active)
        )
        return self._persistence.create_workflow(bounded)

    def get(self, owner_user_id: int, workflow_id: int) -> WorkflowExecution:
        return self._persistence.get_workflow(owner_user_id=owner_user_id, workflow_id=workflow_id)

    def detail(self, owner_user_id: int, workflow_id: int) -> WorkflowDetail:
        return self._persistence.detail(owner_user_id=owner_user_id, workflow_id=workflow_id)

    def list(
        self, owner_user_id: int, *, limit: int, before_id: int | None
    ) -> tuple[WorkflowExecution, ...]:
        if not 1 <= limit <= WORKFLOW_PAGE_LIMIT_MAX:
            raise ValueError("limit must be between 1 and 100")
        if before_id is not None and before_id <= 0:
            raise ValueError("before_id must be positive")
        return self._persistence.list_workflows(
            owner_user_id=owner_user_id, limit=limit, before_id=before_id
        )

    def pause(self, owner_user_id: int, workflow_id: int) -> WorkflowExecution:
        """Stop accepting further steps at the next boundary. An in-flight Run still finishes.

        Pause is a durable flag, not a cancellation and not a synthetic signal: resuming
        must never fabricate the wakeup the pause withheld.
        """
        return self._persistence.set_paused(
            owner_user_id=owner_user_id,
            workflow_id=workflow_id,
            paused=True,
            now=require_utc(self._clock()),
        )

    def resume(self, owner_user_id: int, workflow_id: int) -> WorkflowExecution:
        """Clear the pause flag on a still-eligible workflow, and nothing else.

        A terminal workflow cannot be resumed: `needs_review` in particular is the state a
        human must act on deliberately, so it takes an explicit owner decision rather than a
        flag flip.
        """
        return self._persistence.set_paused(
            owner_user_id=owner_user_id,
            workflow_id=workflow_id,
            paused=False,
            now=require_utc(self._clock()),
        )

    def cancel(self, owner_user_id: int, workflow_id: int) -> WorkflowExecution:
        """Stop the workflow permanently. Cancellation of a live Run is a separate,
        existing authority and is deliberately not triggered from here."""
        return self._persistence.cancel(
            owner_user_id=owner_user_id, workflow_id=workflow_id, now=require_utc(self._clock())
        )

    def signal(
        self,
        owner_user_id: int,
        workflow_id: int,
        *,
        signal_key: str,
        payload: Mapping[str, JsonValue],
        expected_revision: int,
    ) -> WorkflowSignalOutcome:
        """Deliver one authenticated owner wakeup.

        Identical replay returns the prior outcome without advancing twice; changed content
        under the same key is a conflict; a stale expected revision cannot advance the wrong
        wait.
        """
        validate_safe_key(signal_key, "signal key")
        if expected_revision < 0:
            raise ValueError("expected_revision must not be negative")
        return self._persistence.deliver_signal(
            owner_user_id=owner_user_id,
            workflow_id=workflow_id,
            signal_key=signal_key,
            payload=freeze_signal(dict(payload)),
            expected_revision=expected_revision,
            now=require_utc(self._clock()),
        )

    def decide(
        self,
        owner_user_id: int,
        workflow_id: int,
        decision_id: int,
        *,
        approve: bool,
        expected_revision: int,
    ) -> WorkflowDecisionView:
        """Record one owner decision about proposed work.

        This grants nothing. It does not create a tool grant, does not reveal a credential,
        and does not stand in for the live-Attempt approval ADR 0037 still requires at
        dispatch.
        """
        if expected_revision < 0:
            raise ValueError("expected_revision must be nonnegative")
        return self._persistence.decide(
            owner_user_id=owner_user_id,
            workflow_id=workflow_id,
            decision_id=decision_id,
            approve=approve,
            expected_revision=expected_revision,
            now=require_utc(self._clock()),
        )

    def list_decisions(
        self, owner_user_id: int, workflow_id: int
    ) -> tuple[WorkflowDecisionView, ...]:
        return self._persistence.list_decisions(
            owner_user_id=owner_user_id, workflow_id=workflow_id
        )


# ---------------------------------------------------------------------------------------
# Scheduler tick
# ---------------------------------------------------------------------------------------


class WorkflowContinuationService:
    """The bounded, fair workflow tick the Scheduler runs alongside its schedule scan.

    It executes no model, no tool and no package code, and it never creates a second queue:
    a continuation is an ordinary Run inserted through the canonical primitive, so it
    competes for exactly the same admission and concurrency as any other submission.
    """

    def __init__(
        self,
        persistence: WorkflowContinuationPersistence,
        *,
        clock: Clock,
        scan_limit: int = WORKFLOW_TICK_SCAN_LIMIT,
        dispatch_limit: int = WORKFLOW_TICK_DISPATCH_LIMIT,
    ) -> None:
        if scan_limit < 1 or dispatch_limit < 1:
            raise ValueError("workflow tick limits must be positive")
        self._persistence = persistence
        self._clock = clock
        self._scan_limit = scan_limit
        self._dispatch_limit = dispatch_limit

    def tick(self) -> WorkflowTickReport:
        return self._persistence.tick(now=require_utc(self._clock()))


# ---------------------------------------------------------------------------------------
# Shared validation used by both persistence modules
# ---------------------------------------------------------------------------------------


def step_input_text(workflow_kind: str, step_number: int, revision: int) -> str:
    """The bounded, credential-free `runs.input_text` for one workflow step.

    The application state does **not** travel here: it travels through the immutable
    workflow snapshot the Worker mediates, because `runs.input_text` is bounded by the
    ordinary Run input limits while a checkpoint has its own 64 KiB bound. Keeping them
    separate is what stops a checkpoint from being smuggled through a smaller column.
    """
    return canonical_json_text(
        {"workflow": workflow_kind, "step": step_number, "revision": revision}
    )


def reservation_for(limits: RunLimits) -> WorkflowReservation:
    """The whole per-Run capacity this step reserves before it is accepted."""
    return WorkflowReservation(
        model_calls=limits.max_model_calls,
        tool_calls=limits.max_tool_calls,
        output_tokens=limits.max_model_calls * limits.max_output_tokens,
    )


def budget_allows(budget: WorkflowBudget, used_steps: int) -> bool:
    return used_steps < budget.max_steps


def deadline_allows(budget: WorkflowBudget, workflow: WorkflowExecution, now: datetime) -> bool:
    return now < workflow.deadline_at


def within_deadline_hours(hours: int) -> bool:
    """Whether an owner-requested deadline fits the frozen 24h default / seven-day maximum."""
    return 1 <= hours <= DEADLINE_HOURS_HARD


__all__ = [
    "DEADLINE_HOURS_HARD",
    "INVALID_REVIEW_REASONS",
    "REVIEW_AUTHORITY_LOST",
    "REVIEW_RUN_TERMINAL",
    "SIGNAL_ACCEPTED",
    "SIGNAL_CANCELLED",
    "SIGNAL_NOT_WAITING",
    "SIGNAL_PAYLOAD_CONFLICT",
    "SIGNAL_REPLAYED",
    "SIGNAL_STALE_REVISION",
    "TERMINAL_WORKFLOW_STATUSES",
    "WORKFLOW_JOB_MAX_ATTEMPTS",
    "WORKFLOW_PAGE_LIMIT_MAX",
    "WORKFLOW_STEP_PAGE_LIMIT_MAX",
    "WORKFLOW_TICK_DISPATCH_LIMIT",
    "WORKFLOW_TICK_SCAN_LIMIT",
    "WorkflowBudget",
    "WorkflowCapacityExceeded",
    "WorkflowContinuationPersistence",
    "WorkflowContinuationService",
    "WorkflowCreationRequest",
    "WorkflowDecisionRequest",
    "WorkflowDecisionState",
    "WorkflowDecisionView",
    "WorkflowDetail",
    "WorkflowDirective",
    "WorkflowExecution",
    "WorkflowNotFound",
    "WorkflowPersistence",
    "WorkflowReservation",
    "WorkflowReservations",
    "WorkflowService",
    "WorkflowSignalOutcome",
    "WorkflowSignalView",
    "WorkflowStatus",
    "WorkflowStep",
    "WorkflowStepCommitter",
    "WorkflowStepResult",
    "WorkflowStepView",
    "WorkflowTickReport",
    "WorkflowTransitionError",
    "WorkflowWaitKind",
    "accepts_reservation",
    "assert_transition",
    "budget_allows",
    "deadline_allows",
    "reservation_for",
    "step_input_text",
    "within_deadline_hours",
]
