"""W1 durable autonomous workflow domain values, bounds, and lifecycle validation.

This module depends only on primitives and on the frozen `JsonValue` contract that
Stage D already established. It holds no database session, no I/O, and no knowledge
of Runs, Jobs or Attempts: a WorkflowExecution *references* ordinary Runs, it does
not own execution authority.

Everything here is derived from `docs/adr/0039-durable-autonomous-workflows.md` and
`docs/autonomous-workflows/workflow-contract.md`.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import cast

from nervos_core.domain.tools import (
    InvalidToolDefinition,
    JsonScalar,
    JsonValue,
    canonical_json_text,
    require_json_value,
)

# ---------------------------------------------------------------------------------------
# Frozen bounds (ADR 0039). Every one of these is also enforced by a database CHECK or by
# validation before persistence, so a value outside them cannot exist durably even if a
# caller bypasses this module.
# ---------------------------------------------------------------------------------------

MAX_STEPS_DEFAULT = 32
MAX_STEPS_HARD = 128
MAX_ACTIVE_WORKFLOWS_DEFAULT = 16
MAX_ACTIVE_WORKFLOWS_HARD = 64
CHECKPOINT_MAX_BYTES = 64 * 1024
JSON_MAX_DEPTH = 16
JSON_MAX_NODES = 4096
SIGNAL_MAX_BYTES = 8 * 1024
SUMMARY_MAX_CHARS = 512
MODEL_CALL_RESERVATION_DEFAULT = 256
MODEL_CALL_RESERVATION_HARD = 2048
TOOL_CALL_RESERVATION_DEFAULT = 256
TOOL_CALL_RESERVATION_HARD = 2048
OUTPUT_TOKEN_RESERVATION_DEFAULT = 262_144
OUTPUT_TOKEN_RESERVATION_HARD = 2_097_152
DEADLINE_HOURS_DEFAULT = 24
DEADLINE_HOURS_HARD = 168  # seven days
MAX_RETAINED_CHECKPOINTS = 128
#: The one workflow state schema version this build speaks. A package that declares any
#: other version is refused rather than reinterpreted: silently coercing an unknown state
#: shape is how a checkpoint round trip quietly loses data.
WORKFLOW_STATE_SCHEMA_VERSION = 1
#: Every workflow Job gets exactly one Attempt (ADR 0039). An Attempt that fails is terminal:
#: the workflow projects to `needs_review` rather than being retried by the ordinary C3
#: retry ladder, because a failed step may already have caused an external effect.
WORKFLOW_JOB_MAX_ATTEMPTS = 1

#: The opaque-key alphabet. It is exactly the alphabet the database CHECK on
#: `workflow_executions.submission_key` accepts, so a key this validator accepts is always
#: storable and one the database would have accepted is never rejected here first.
_SAFE_KEY = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


#: The immutable shape a package actually receives. An object is a read-only mapping and an
#: array is a tuple, because a checkpoint that a package could mutate in place would make the
#: stored digest a lie. Mirrors the SDK's own `ImmutableJSONValue` for the same reason.
type FrozenJSONValue = JsonScalar | Mapping[str, "FrozenJSONValue"] | tuple["FrozenJSONValue", ...]
type FrozenState = Mapping[str, FrozenJSONValue]


class InvalidWorkflow(ValueError):
    """A durable workflow value violates the frozen W1 contract."""


class WorkflowNotFound(LookupError):
    """The workflow does not exist, or belongs to another owner."""


class WorkflowTransitionError(WorkflowNotFound):
    """The requested transition is not legal from the workflow's current state."""


class WorkflowConflict(ValueError):
    """The request conflicts with durable state (revision, digest, or duplicate identity)."""


class WorkflowNotLinked(LookupError):
    """The referenced Run has no workflow step, so it is an ordinary Run.

    Raised rather than silently ignored so the fenced boundary can refuse a workflow write
    for a Run that never was one, instead of inventing a workflow for it.
    """


class WorkflowStatus(StrEnum):
    """Closed workflow lifecycle vocabulary. Distinct from `RunStatus` by construction."""

    PENDING = "pending"
    RUNNABLE = "runnable"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_REVIEW = "needs_review"


TERMINAL_WORKFLOW_STATUSES: frozenset[WorkflowStatus] = frozenset(
    {
        WorkflowStatus.SUCCEEDED,
        WorkflowStatus.FAILED,
        WorkflowStatus.CANCELLED,
        WorkflowStatus.NEEDS_REVIEW,
    }
)
#: States in which a Scheduler tick may accept a new continuation Run. `needs_review` is
#: excluded on purpose: it is the "a human must look at this" state, so automatic
#: progression resuming from it would defeat the entire point of having it.
DISPATCHABLE_WORKFLOW_STATUSES: frozenset[WorkflowStatus] = frozenset(
    {WorkflowStatus.PENDING, WorkflowStatus.RUNNABLE, WorkflowStatus.WAITING}
)


class WorkflowWaitKind(StrEnum):
    """What a `waiting` workflow is waiting for. Each kind has its own wake rule."""

    TIME = "time"
    SIGNAL = "signal"
    OWNER_DECISION = "owner_decision"


class WorkflowDirectiveKind(StrEnum):
    COMPLETE = "complete"
    NEXT = "next"
    WAIT = "wait"


class WorkflowStepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowDecisionState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    CONSUMED = "consumed"


DECIDABLE_DECISION_STATES: frozenset[WorkflowDecisionState] = frozenset(
    {WorkflowDecisionState.PENDING}
)

#: Reason codes for `needs_review`. Bounded, machine-checkable, and safe to show.
REVIEW_CONFIGURATION_CHANGED = "configuration_changed"
REVIEW_AMBIGUOUS_EXTERNAL_EFFECT = "ambiguous_external_effect"
REVIEW_RUN_TERMINAL = "run_terminal_without_progress"
REVIEW_AUTHORITY_LOST = "authority_lost"
REVIEW_DEADLINE_EXCEEDED = "deadline_exceeded"
REVIEW_BUDGET_EXHAUSTED = "budget_exhausted"
REVIEW_STEP_LIMIT = "step_limit_reached"

#: The closed set of machine-checkable reasons a workflow can require human attention. A
#: `needs_review` row whose reason is outside this set is a persistence defect, so the
#: database CHECK below and this tuple are kept as one contract.
INVALID_REVIEW_REASONS: frozenset[str] = frozenset(
    {
        REVIEW_CONFIGURATION_CHANGED,
        REVIEW_AMBIGUOUS_EXTERNAL_EFFECT,
        REVIEW_RUN_TERMINAL,
        REVIEW_AUTHORITY_LOST,
        REVIEW_DEADLINE_EXCEEDED,
        REVIEW_BUDGET_EXHAUSTED,
        REVIEW_STEP_LIMIT,
    }
)


# ---------------------------------------------------------------------------------------
# Bounded JSON state
# ---------------------------------------------------------------------------------------


def _measure(value: object, *, depth: int = 1, nodes: list[int] | None = None) -> int:
    """Return the node count of a bounded JSON value, refusing depth and non-finite numbers.

    Counts every scalar and container as one node. A value that exceeds either bound is
    refused *before* it is serialized, so an oversized checkpoint never reaches a frame
    writer, a column, or an API response.
    """
    counter = nodes if nodes is not None else [0]
    if depth > JSON_MAX_DEPTH:
        raise InvalidWorkflow("workflow state exceeds the depth bound")
    counter[0] += 1
    if counter[0] > JSON_MAX_NODES:
        raise InvalidWorkflow("workflow state exceeds the node bound")
    # `bool` is an `int` subclass, so the `isinstance(value, float)` half is what keeps a
    # boolean from being checked as a number at all.
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise InvalidWorkflow("workflow state must contain finite numbers only")
    if isinstance(value, Mapping):
        for key, item in cast("Mapping[object, object]", value).items():
            if not isinstance(key, str):
                raise InvalidWorkflow("workflow state keys must be strings")
            _measure(item, depth=depth + 1, nodes=counter)
        return counter[0]
    if isinstance(value, list | tuple):
        for item in cast("tuple[object, ...]", value):
            _measure(item, depth=depth + 1, nodes=counter)
        return counter[0]
    return counter[0]


def freeze_state(value: object) -> FrozenState:
    """Validate one application state object and return an immutable deep copy.

    Rejects a non-object root, non-string keys, values that are not JSON at all,
    non-finite numbers, and anything over the depth, node, or byte bounds. An **empty**
    state is legal: a step that needs no retained state is not a degenerate workflow.
    """
    state = _require_state_object(value)
    _measure(state)
    text = canonical_json_text(state)
    if len(text.encode("utf-8")) > CHECKPOINT_MAX_BYTES:
        raise InvalidWorkflow("workflow state exceeds the byte bound")
    return cast("FrozenState", _freeze(state))


def _require_state_object(value: object) -> dict[str, JsonValue]:
    """Apply the Stage-D `JsonValue` contract to a state *root*, as a JSON object.

    The underlying contract already refuses non-finite numbers, non-string keys, and
    non-JSON types. Re-raising it as `InvalidWorkflow` keeps one exception type for every
    workflow-state refusal, so a caller does not have to know that the JSON contract is
    shared with the tool schema.
    """
    state = _require_state_json(value)
    if not isinstance(state, dict):
        raise InvalidWorkflow("workflow state must be a JSON object")
    return state


def _require_state_json(value: object) -> JsonValue:
    """Validate one candidate state against the Stage-D JSON contract.

    The input is normalized first, because a package routinely returns the snapshot it was
    *given* as its next state, and that snapshot is already frozen: objects are read-only
    mappings and arrays are tuples. Refusing that shape would make the ordinary round trip
    -- state out, unchanged state back -- the one case that fails.
    """
    try:
        return require_json_value(_as_plain_json(value))
    except InvalidToolDefinition as error:
        raise InvalidWorkflow(f"workflow state is not valid JSON: {error}") from error


def _as_plain_json(value: object) -> JsonValue:
    """Convert any accepted state shape into the plain `dict`/`list` JSON contract.

    Handles both a freshly built structure and an already-frozen one, so `freeze_state` is
    idempotent: freezing a frozen snapshot yields the identical digest.
    """
    if isinstance(value, Mapping):
        return {
            str(k): _as_plain_json(v) for k, v in cast("Mapping[object, object]", value).items()
        }
    if isinstance(value, list | tuple):
        return [_as_plain_json(item) for item in cast("tuple[object, ...]", value)]
    return cast("JsonValue", value)


def _freeze(value: JsonValue) -> FrozenJSONValue:
    if isinstance(value, dict):
        return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def thaw_state(value: FrozenJSONValue) -> JsonValue:
    """Return a plain, JSON-serializable copy of an immutable state value.

    `canonical_json_text` speaks the Stage-D `JsonValue` contract, in which an object is a
    `dict` and an array is a `list`. The immutable shape a package receives uses
    `MappingProxyType` and tuples, so it has to be converted back before serialization.
    Doing that in exactly one place keeps every digest and every byte-bound check on the
    same canonical bytes.
    """
    if isinstance(value, Mapping):
        return {str(k): thaw_state(cast("FrozenJSONValue", v)) for k, v in value.items()}
    if isinstance(value, tuple):
        return [thaw_state(item) for item in value]
    return cast("JsonValue", value)


def state_digest(state: Mapping[str, FrozenJSONValue]) -> str:
    """SHA-256 of the canonical JSON of one state object. The same identity for the same state."""
    return hashlib.sha256(canonical_json_text(thaw_state(state)).encode("utf-8")).hexdigest()


def content_digest(payload: Mapping[str, FrozenJSONValue]) -> str:
    """SHA-256 of canonical creation content, used for owner-scoped replay identity."""
    return hashlib.sha256(canonical_json_text(thaw_state(payload)).encode("utf-8")).hexdigest()


def freeze_signal(value: object) -> FrozenState:
    """Validate one wakeup signal payload under the tighter 8 KiB signal bound."""
    payload = _require_state_object(value)
    _measure(payload)
    text = canonical_json_text(payload)
    if len(text.encode("utf-8")) > SIGNAL_MAX_BYTES:
        raise InvalidWorkflow("workflow signal exceeds the byte bound")
    return cast("FrozenState", _freeze(payload))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidWorkflow("workflow timestamps must be timezone-aware")
    return value.astimezone(UTC)


def validate_summary(value: str) -> str:
    """A safe, bounded, owner-facing step summary. Never secret, never unbounded."""
    if "\x00" in value or not value.strip() or len(value) > SUMMARY_MAX_CHARS:
        raise InvalidWorkflow("workflow step summary is invalid")
    return value


def validate_safe_key(value: str, field_name: str) -> str:
    """Validate one opaque workflow key: submission identity, signal name, or decision name.

    These are caller-chosen identifiers, not an enum, so the alphabet is the one the schema
    already accepts rather than a stricter invented one. A UUID-shaped key with hyphens is
    therefore a legal submission identity.
    """
    if _SAFE_KEY.fullmatch(value) is None:
        raise InvalidWorkflow(f"invalid workflow {field_name}")
    return value


def validate_digest(value: str, field_name: str) -> str:
    if _DIGEST.fullmatch(value) is None:
        raise InvalidWorkflow(f"invalid workflow {field_name}")
    return value


# ---------------------------------------------------------------------------------------
# Directives
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkflowDirective:
    """What one bounded step decided to do next.

    Field exclusivity is enforced in `__post_init__`, not by the caller. `complete` and
    `next` carry nothing at all, because a step that both finished and scheduled something
    has no honest single interpretation and must fail closed.
    """

    kind: WorkflowDirectiveKind
    wait_kind: WorkflowWaitKind | None = None
    wakeup_at: datetime | None = None
    wait_seconds: int | None = None
    signal_key: str | None = None
    decision_key: str | None = None

    def __post_init__(self) -> None:
        if self.kind is WorkflowDirectiveKind.COMPLETE:
            if any(
                value is not None
                for value in (self.wait_kind, self.wakeup_at, self.wait_seconds, self.signal_key)
            ):
                raise InvalidWorkflow("a completing directive carries no wait fields")
            return
        if self.kind is WorkflowDirectiveKind.NEXT:
            if any(
                value is not None
                for value in (self.wait_kind, self.wakeup_at, self.wait_seconds, self.signal_key)
            ):
                raise InvalidWorkflow("a next-step directive carries no wait fields")
            return
        if self.wait_kind is WorkflowWaitKind.TIME:
            if self.signal_key is not None or self.decision_key is not None:
                raise InvalidWorkflow("a time wait carries no signal identity")
            if (self.wakeup_at is None) == (self.wait_seconds is None):
                raise InvalidWorkflow("a time wait needs exactly one of wakeup_at or wait_seconds")
            if self.wakeup_at is not None:
                object.__setattr__(self, "wakeup_at", _utc(self.wakeup_at))
            if self.wait_seconds is not None and not 1 <= self.wait_seconds <= 30 * 24 * 3600:
                raise InvalidWorkflow("wait_seconds is outside its bounds")
            return
        if self.wait_kind is WorkflowWaitKind.SIGNAL:
            if self.wakeup_at is not None or self.wait_seconds is not None:
                raise InvalidWorkflow("a signal wait carries no time field")
            if self.signal_key is None:
                raise InvalidWorkflow("a signal wait requires signal_key")
            validate_safe_key(self.signal_key, "signal key")
            return
        if self.wait_kind is WorkflowWaitKind.OWNER_DECISION:
            if self.wakeup_at is not None or self.wait_seconds is not None:
                raise InvalidWorkflow("a decision wait carries no time field")
            if self.decision_key is None:
                raise InvalidWorkflow("a decision wait requires decision_key")
            validate_safe_key(self.decision_key, "decision key")
            return
        raise InvalidWorkflow("a wait directive requires a wait kind")

    @property
    def completes(self) -> bool:
        return self.kind is WorkflowDirectiveKind.COMPLETE

    @property
    def waits(self) -> bool:
        return self.kind is WorkflowDirectiveKind.WAIT


def waiting_directive(now: datetime, *, seconds: int) -> WorkflowDirective:
    return WorkflowDirective(
        WorkflowDirectiveKind.WAIT,
        wait_kind=WorkflowWaitKind.TIME,
        wakeup_at=_utc(now) + timedelta(seconds=seconds),
    )


def signal_directive(key: str) -> WorkflowDirective:
    return WorkflowDirective(
        WorkflowDirectiveKind.WAIT, wait_kind=WorkflowWaitKind.SIGNAL, signal_key=key
    )


def decision_directive(key: str) -> WorkflowDirective:
    return WorkflowDirective(
        WorkflowDirectiveKind.WAIT, wait_kind=WorkflowWaitKind.OWNER_DECISION, decision_key=key
    )


def next_directive() -> WorkflowDirective:
    return WorkflowDirective(WorkflowDirectiveKind.NEXT)


def complete_directive() -> WorkflowDirective:
    return WorkflowDirective(WorkflowDirectiveKind.COMPLETE)


def resolved_wakeup_at(directive: WorkflowDirective, now: datetime) -> datetime | None:
    """Resolve a directive's absolute wakeup instant, or None when it is not time-based."""
    if not directive.waits or directive.wait_kind is not WorkflowWaitKind.TIME:
        return None
    if directive.wakeup_at is not None:
        return directive.wakeup_at
    assert directive.wait_seconds is not None
    return _utc(now) + timedelta(seconds=directive.wait_seconds)


# ---------------------------------------------------------------------------------------
# Owner decisions
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkflowDecisionRequest:
    """One exact proposed external action a step wants an owner to authorize.

    This is *proposed application work*, not permission to dispatch. It binds the owner,
    the workflow and checkpoint revision it was observed at, the exact tool identity and
    reviewed schema fingerprint, a canonical digest of the exact arguments, the pinned
    package and configuration identity in force when it was proposed, and an expiry.
    Editing any of those invalidates it.
    """

    checkpoint_revision: int
    tool_definition_id: int
    upstream_name: str
    action_fingerprint: str
    arguments: Mapping[str, FrozenJSONValue]
    arguments_digest: str
    preview: Mapping[str, FrozenJSONValue]
    package_content_digest: str | None
    effective_config_digest: str | None
    expires_at: datetime

    def __post_init__(self) -> None:
        if self.checkpoint_revision < 0:
            raise InvalidWorkflow("decision checkpoint revision must not be negative")
        if self.tool_definition_id <= 0:
            raise InvalidWorkflow("decision requires a tool definition")
        if not self.upstream_name or len(self.upstream_name) > 128:
            raise InvalidWorkflow("decision upstream name is invalid")
        validate_digest(self.action_fingerprint, "action fingerprint")
        validate_digest(self.arguments_digest, "arguments digest")
        if decision_arguments_digest(self.arguments) != self.arguments_digest:
            raise InvalidWorkflow("decision arguments do not match their digest")
        _measure(dict(self.arguments))
        _measure(dict(self.preview))
        preview_text = canonical_json_text(thaw_state(dict(self.preview)))
        if len(preview_text.encode("utf-8")) > CHECKPOINT_MAX_BYTES:
            raise InvalidWorkflow("decision preview exceeds the byte bound")
        object.__setattr__(self, "arguments", _freeze_mapping(dict(self.arguments)))
        object.__setattr__(self, "preview", _freeze_mapping(dict(self.preview)))
        object.__setattr__(self, "expires_at", _utc(self.expires_at))
        if self.package_content_digest is not None:
            validate_digest(self.package_content_digest, "package content digest")
        if self.effective_config_digest is not None:
            validate_digest(self.effective_config_digest, "configuration digest")


def _freeze_mapping(value: Mapping[str, FrozenJSONValue]) -> Mapping[str, FrozenJSONValue]:
    """Freeze an already-validated immutable mapping into the canonical read-only form."""
    return cast(
        "Mapping[str, FrozenJSONValue]",
        _freeze(cast("JsonValue", {str(k): v for k, v in value.items()})),
    )


def decision_arguments_digest(arguments: Mapping[str, FrozenJSONValue]) -> str:
    return hashlib.sha256(canonical_json_text(thaw_state(arguments)).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkflowBudget:
    """Cumulative, never-refunded reservations for one workflow.

    Every field is a *reservation* drawn down as steps are accepted, not a limit on any
    single step. That distinction is the whole reason a restart or a crash cannot buy free
    work: the counter is durable, and an unaccepted step refunds nothing.
    """

    max_steps: int = MAX_STEPS_DEFAULT
    model_call_reservation: int = MODEL_CALL_RESERVATION_DEFAULT
    tool_call_reservation: int = TOOL_CALL_RESERVATION_DEFAULT
    output_token_reservation: int = OUTPUT_TOKEN_RESERVATION_DEFAULT
    deadline_hours: int = DEADLINE_HOURS_DEFAULT
    max_retained_checkpoints: int = MAX_RETAINED_CHECKPOINTS

    def __post_init__(self) -> None:
        if not 1 <= self.max_steps <= MAX_STEPS_HARD:
            raise InvalidWorkflow("max_steps is outside its bounds")
        if not 1 <= self.model_call_reservation <= MODEL_CALL_RESERVATION_HARD:
            raise InvalidWorkflow("model call reservation is outside its bounds")
        if not 0 <= self.tool_call_reservation <= TOOL_CALL_RESERVATION_HARD:
            raise InvalidWorkflow("tool call reservation is outside its bounds")
        if not 1 <= self.output_token_reservation <= OUTPUT_TOKEN_RESERVATION_HARD:
            raise InvalidWorkflow("output token reservation is outside its bounds")
        if not 1 <= self.deadline_hours <= DEADLINE_HOURS_HARD:
            raise InvalidWorkflow("deadline is outside its bounds")
        if not 1 <= self.max_retained_checkpoints <= MAX_RETAINED_CHECKPOINTS:
            raise InvalidWorkflow("retained checkpoint bound is outside its bounds")

    def deadline_at(self, created_at: datetime) -> datetime:
        return _utc(created_at) + timedelta(hours=self.deadline_hours)


@dataclass(frozen=True, slots=True)
class WorkflowReservations:
    """What has already been drawn down. Never decreases, never resets across a restart."""

    model_calls: int = 0
    tool_calls: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        if self.model_calls < 0 or self.tool_calls < 0 or self.output_tokens < 0:
            raise InvalidWorkflow("reservations must be nonnegative")

    def remaining(self, budget: WorkflowBudget) -> tuple[int, int, int]:
        return (
            budget.model_call_reservation - self.model_calls,
            budget.tool_call_reservation - self.tool_calls,
            budget.output_token_reservation - self.output_tokens,
        )


@dataclass(frozen=True, slots=True)
class WorkflowReservation:
    """One accepted step's share of the cumulative reservations."""

    model_calls: int
    tool_calls: int
    output_tokens: int

    def __post_init__(self) -> None:
        if min(self.model_calls, self.tool_calls, self.output_tokens) < 0:
            raise InvalidWorkflow("a reservation must be nonnegative")


def accepts_reservation(
    reservations: WorkflowReservations, budget: WorkflowBudget, claim: WorkflowReservation
) -> bool:
    """Whether the remaining cumulative reservation covers one step's whole capacity."""
    models, tools, tokens = reservations.remaining(budget)
    return (
        claim.model_calls <= models and claim.tool_calls <= tools and claim.output_tokens <= tokens
    )


# ---------------------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkflowExecution:
    """One owner-scoped durable workflow. Not a Run, not a Conversation, not memory."""

    id: int
    owner_user_id: int
    agent_instance_id: int
    workflow_kind: str
    state_schema_version: int
    submission_key: str
    submission_digest: str
    status: WorkflowStatus
    paused: bool
    checkpoint_revision: int
    step_count: int
    budget: WorkflowBudget
    reservations: WorkflowReservations
    deadline_at: datetime
    wait_kind: WorkflowWaitKind | None = None
    wakeup_at: datetime | None = None
    signal_key: str | None = None
    decision_key: str | None = None
    review_reason: str | None = None
    # Pinned executable identity, snapshotted at creation and re-verified before every
    # continuation. A change is `needs_review`, never an automatic re-pin.
    agent_key: str = ""
    agent_definition_version: str = ""
    model_provider: str = ""
    model_name: str = ""
    package_content_digest: str | None = None
    package_environment_digest: str | None = None
    effective_config_digest: str | None = None
    agent_instance_config_revision: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    finished_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.id <= 0 or self.owner_user_id <= 0 or self.agent_instance_id <= 0:
            raise InvalidWorkflow("workflow identifiers must be positive")
        validate_safe_key(self.workflow_kind, "workflow kind")
        validate_safe_key(self.submission_key, "submission key")
        validate_digest(self.submission_digest, "submission digest")
        if self.state_schema_version != WORKFLOW_STATE_SCHEMA_VERSION:
            raise InvalidWorkflow("unsupported workflow state schema version")
        if self.checkpoint_revision < 0 or self.step_count < 0:
            raise InvalidWorkflow("workflow revisions and step counts must be nonnegative")
        if self.step_count > self.budget.max_steps:
            raise InvalidWorkflow("workflow exceeded its step budget")
        if self.status is WorkflowStatus.WAITING and self.wait_kind is None:
            raise InvalidWorkflow("a waiting workflow must record what it waits for")
        if self.status is not WorkflowStatus.WAITING and self.wait_kind is not None:
            raise InvalidWorkflow("only a waiting workflow records a wait kind")
        if self.wait_kind is WorkflowWaitKind.TIME and self.wakeup_at is None:
            raise InvalidWorkflow("a time wait requires a wakeup instant")
        if self.signal_key is not None:
            validate_safe_key(self.signal_key, "signal key")
        if self.decision_key is not None:
            validate_safe_key(self.decision_key, "decision key")
        for name in ("deadline_at", "created_at", "updated_at", "finished_at", "wakeup_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _utc(value))
        if self.status in TERMINAL_WORKFLOW_STATUSES and self.finished_at is None:
            raise InvalidWorkflow("a terminal workflow requires a finish boundary")

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_WORKFLOW_STATUSES

    @property
    def is_dispatchable(self) -> bool:
        return self.status in DISPATCHABLE_WORKFLOW_STATUSES and not self.paused


@dataclass(frozen=True, slots=True)
class WorkflowStep:
    """One step: a pinned checkpoint plus the single ordinary Run that executes it."""

    id: int
    workflow_id: int
    step_number: int
    run_id: int | None
    expected_checkpoint_revision: int
    state_version: int
    state: FrozenState
    state_digest: str
    status: WorkflowStepStatus
    summary: str | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.id <= 0 or self.workflow_id <= 0 or self.step_number <= 0:
            raise InvalidWorkflow("workflow step identifiers must be positive")
        if self.run_id is not None and self.run_id <= 0:
            raise InvalidWorkflow("workflow step run id must be positive")
        if self.expected_checkpoint_revision < 0:
            raise InvalidWorkflow("expected checkpoint revision must not be negative")
        if self.state_version != WORKFLOW_STATE_SCHEMA_VERSION:
            raise InvalidWorkflow("unsupported workflow state schema version")
        validate_digest(self.state_digest, "state digest")
        if state_digest(self.state) != self.state_digest:
            raise InvalidWorkflow("workflow step state does not match its digest")
        if self.summary is not None:
            validate_summary(self.summary)
        for name in ("created_at", "finished_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _utc(value))


@dataclass(frozen=True, slots=True)
class WorkflowCheckpoint:
    """One immutable committed revision of application state. Append-only evidence."""

    id: int
    workflow_id: int
    revision: int
    step_number: int
    state: FrozenState
    state_digest: str
    created_at: datetime

    def __post_init__(self) -> None:
        if self.id <= 0 or self.workflow_id <= 0 or self.revision < 0 or self.step_number <= 0:
            raise InvalidWorkflow("workflow checkpoint identifiers must be positive")
        validate_digest(self.state_digest, "state digest")
        if state_digest(self.state) != self.state_digest:
            raise InvalidWorkflow("workflow checkpoint state does not match its digest")
        object.__setattr__(self, "created_at", _utc(self.created_at))


@dataclass(frozen=True, slots=True)
class WorkflowStepResult:
    """One bounded step outcome, fully validated before any terminalization.

    Validation happens here, at the Worker boundary, *before* the fenced success write.
    That ordering is deliberate: package data that violates the bounds must fail the Run
    cleanly rather than strand a Run that is already `succeeded` with no checkpoint.
    """

    directive: WorkflowDirective
    state: FrozenState
    summary: str | None = None
    decision_request: WorkflowDecisionRequest | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", freeze_state(dict(self.state)))
        if self.summary is not None:
            object.__setattr__(self, "summary", validate_summary(self.summary))
        if (
            self.decision_request is None
            and self.directive.waits
            and self.directive.wait_kind is WorkflowWaitKind.OWNER_DECISION
        ):
            raise InvalidWorkflow("a decision wait requires a decision request")
        if self.decision_request is not None and not self.directive.waits:
            raise InvalidWorkflow("a decision request requires a decision wait")


# ---------------------------------------------------------------------------------------
# Lifecycle validation
# ---------------------------------------------------------------------------------------

ALLOWED_TRANSITIONS: Mapping[WorkflowStatus, frozenset[WorkflowStatus]] = MappingProxyType(
    {
        WorkflowStatus.PENDING: frozenset(
            {
                WorkflowStatus.RUNNING,
                WorkflowStatus.WAITING,
                WorkflowStatus.SUCCEEDED,
                WorkflowStatus.FAILED,
                WorkflowStatus.CANCELLED,
                WorkflowStatus.NEEDS_REVIEW,
            }
        ),
        WorkflowStatus.RUNNABLE: frozenset(
            {
                WorkflowStatus.RUNNING,
                WorkflowStatus.WAITING,
                WorkflowStatus.SUCCEEDED,
                WorkflowStatus.FAILED,
                WorkflowStatus.CANCELLED,
                WorkflowStatus.NEEDS_REVIEW,
            }
        ),
        WorkflowStatus.RUNNING: frozenset(
            {
                WorkflowStatus.RUNNABLE,
                WorkflowStatus.WAITING,
                WorkflowStatus.SUCCEEDED,
                WorkflowStatus.FAILED,
                WorkflowStatus.CANCELLED,
                WorkflowStatus.NEEDS_REVIEW,
            }
        ),
        WorkflowStatus.WAITING: frozenset(
            {
                WorkflowStatus.RUNNABLE,
                WorkflowStatus.RUNNING,
                WorkflowStatus.SUCCEEDED,
                WorkflowStatus.FAILED,
                WorkflowStatus.CANCELLED,
                WorkflowStatus.NEEDS_REVIEW,
            }
        ),
        WorkflowStatus.SUCCEEDED: frozenset(),
        WorkflowStatus.FAILED: frozenset(),
        WorkflowStatus.CANCELLED: frozenset(),
        WorkflowStatus.NEEDS_REVIEW: frozenset(),
    }
)


def assert_transition(source: WorkflowStatus, target: WorkflowStatus) -> None:
    """Refuse an illegal workflow transition. Terminal states are immutable."""
    if target not in ALLOWED_TRANSITIONS[source]:
        raise WorkflowTransitionError(f"cannot move a {source.value} workflow to {target.value}")


def active_workflow_count_limit(limit: int) -> int:
    if not 1 <= limit <= MAX_ACTIVE_WORKFLOWS_HARD:
        raise InvalidWorkflow("active workflow limit is outside its bounds")
    return limit


def checkpoint_retention(committed: int, limit: int) -> int:
    """Return how many checkpoints to retain: the newest `limit`, never silently zero.

    A terminal workflow retains its last `max_retained_checkpoints` revisions so the owner
    can still inspect *why* it stopped; anything older is dropped by explicit retention,
    never by an incidental overwrite.
    """
    if committed < 0:
        raise InvalidWorkflow("committed checkpoint count must be nonnegative")
    return min(committed, limit)


__all__ = [
    "ALLOWED_TRANSITIONS",
    "CHECKPOINT_MAX_BYTES",
    "DEADLINE_HOURS_DEFAULT",
    "DEADLINE_HOURS_HARD",
    "DECIDABLE_DECISION_STATES",
    "DISPATCHABLE_WORKFLOW_STATUSES",
    "JSON_MAX_DEPTH",
    "JSON_MAX_NODES",
    "MAX_ACTIVE_WORKFLOWS_DEFAULT",
    "MAX_ACTIVE_WORKFLOWS_HARD",
    "MAX_RETAINED_CHECKPOINTS",
    "MAX_STEPS_DEFAULT",
    "MAX_STEPS_HARD",
    "MODEL_CALL_RESERVATION_DEFAULT",
    "MODEL_CALL_RESERVATION_HARD",
    "OUTPUT_TOKEN_RESERVATION_DEFAULT",
    "OUTPUT_TOKEN_RESERVATION_HARD",
    "REVIEW_AMBIGUOUS_EXTERNAL_EFFECT",
    "REVIEW_AUTHORITY_LOST",
    "REVIEW_CONFIGURATION_CHANGED",
    "REVIEW_RUN_TERMINAL",
    "SIGNAL_MAX_BYTES",
    "SUMMARY_MAX_CHARS",
    "TERMINAL_WORKFLOW_STATUSES",
    "TOOL_CALL_RESERVATION_DEFAULT",
    "TOOL_CALL_RESERVATION_HARD",
    "WORKFLOW_JOB_MAX_ATTEMPTS",
    "WORKFLOW_STATE_SCHEMA_VERSION",
    "InvalidWorkflow",
    "WorkflowBudget",
    "WorkflowCheckpoint",
    "WorkflowConflict",
    "WorkflowDecisionRequest",
    "WorkflowDecisionState",
    "WorkflowDirective",
    "WorkflowDirectiveKind",
    "WorkflowExecution",
    "WorkflowNotFound",
    "WorkflowNotLinked",
    "WorkflowReservation",
    "WorkflowReservations",
    "WorkflowStatus",
    "WorkflowStep",
    "WorkflowStepResult",
    "WorkflowStepStatus",
    "WorkflowTransitionError",
    "WorkflowWaitKind",
    "accepts_reservation",
    "active_workflow_count_limit",
    "assert_transition",
    "checkpoint_retention",
    "complete_directive",
    "content_digest",
    "decision_arguments_digest",
    "decision_directive",
    "freeze_signal",
    "freeze_state",
    "next_directive",
    "resolved_wakeup_at",
    "signal_directive",
    "state_digest",
    "thaw_state",
    "validate_digest",
    "validate_safe_key",
    "validate_summary",
    "waiting_directive",
]
