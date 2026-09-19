"""Stage E trigger application ports and the atomic materialization seam.

This module is provider-neutral. It carries no SQLAlchemy connection, no session, no engine, and no
HTTP type: a persistence implementation that mentions a database connection in its *interface* has
made the persistence technology part of the port, and every future implementation, test double and
alternative backend would inherit that.

The load-bearing shape here is the materialization command. It carries **only** information that is
genuinely specific to one occurrence and cannot be derived from durable state, plus the resolved
Agent Definition facts a Run needs. Owner, target Agent Instance, revision and the model-visible
input are deliberately **absent**: the transaction re-reads the trigger and those become
authoritative there. Carrying them here would create two possible authorities for the same fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from nervos_core.application.agent_definitions import AgentDefinitionResolver
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.runs import RunLimits
from nervos_core.domain.triggers import (
    InvalidTrigger,
    MisfirePolicy,
    OccurrenceStatus,
    ScheduleSpec,
    SkipReason,
    TriggerDefinition,
    TriggerKind,
    TriggerOccurrence,
    validate_event_type,
)

#: The claim budget a triggered Run receives. It matches the durable submission primitive's own
#: default, so a triggered Run and a manual Run carry the same budget and neither is privileged.
DEFAULT_MAX_ATTEMPTS = 3


class TriggerNotFound(LookupError):
    """The trigger does not exist, or does not belong to the requesting owner."""


class TriggerHasHistory(ValueError):
    """The trigger cannot be deleted while it has occurrences."""


class TriggerNotEditable(ValueError):
    """The requested edit is not permitted for this trigger's current state."""


@dataclass(frozen=True, slots=True)
class ResolvedAgentDefinition:
    """The Agent Definition facts one submission needs, resolved the way a manual Run resolves them.

    A trigger does **not** own model or run limits — the Agent Definition does. This value is the
    output of resolving the trigger's target Agent through the same
    :class:`~nervos_core.application.agent_definitions.AgentDefinitionResolver` a manual submission
    uses, so the two paths cannot diverge.
    """

    definition_id: AgentDefinitionId
    limits: RunLimits


@dataclass(frozen=True, slots=True)
class TriggerEdit:
    """The complete new mutable configuration of a trigger.

    Identity, owner, target Agent Instance and kind are absent by construction, so a kind can never
    change and a trigger can never be retargeted through an edit. Whether a change is *defining* —
    and therefore increments `config_revision` — is decided by comparing this value's fields against
    the stored row, not by the caller.
    """

    display_name: str
    input_text: str
    next_fire_at: datetime | None = None
    schedule: ScheduleSpec | None = None
    event_type: str | None = None


@dataclass(frozen=True, slots=True)
class TriggerDraft:
    """A proposed new trigger, before it has an identity or an owner-scoped row."""

    agent_instance_id: int
    display_name: str
    input_text: str
    kind: TriggerKind
    schedule: ScheduleSpec | None = None
    event_type: str | None = None
    public_id: str | None = None
    secret_digest: bytes | None = None
    secret_created_at: datetime | None = None
    enabled: bool = True
    misfire_policy: MisfirePolicy = MisfirePolicy.COALESCE_ONE
    #: The next instant this trigger is due.
    #:
    #: E1 does **not** compute this. Enforcing the shape is E1's job; deriving the instant from a
    #: schedule is the schedule evaluator's, which E2 introduces. A one-time or interval caller can
    #: compute it trivially; cron genuinely requires the evaluator.
    next_fire_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.kind is TriggerKind.WEBHOOK:
            if self.public_id is None or self.secret_digest is None:
                raise InvalidTrigger("a webhook draft requires a locator and a secret")
        elif self.kind is TriggerKind.EVENT:
            if self.event_type is None:
                raise InvalidTrigger("an event draft requires an event type")
            validate_event_type(self.event_type)
        elif self.schedule is None:
            raise InvalidTrigger("a schedule draft requires a schedule spec")


@dataclass(frozen=True, slots=True)
class TriggerMaterializationCommand:
    """Everything one materialization attempt knows that durable state cannot tell it.

    Deliberately **not** present: `owner_user_id`, `agent_instance_id`, `trigger_revision` and the
    model-visible `input_text`. All four are re-read from the trigger inside the transaction that
    acts on them, so they have exactly one authority. Supplying them here as well would let a stale
    caller value and the durable row disagree, and there would be no principled way to choose.
    """

    trigger_definition_id: int
    definition: ResolvedAgentDefinition
    now: datetime
    occurred_at: datetime
    nominal_at: datetime | None = None
    event_id: str | None = None
    idempotency_key: str | None = None
    payload_digest: bytes | None = None
    payload_bytes: int | None = None

    def __post_init__(self) -> None:
        if self.trigger_definition_id <= 0:
            raise InvalidTrigger("invalid trigger identifier")
        identities = (
            self.nominal_at is not None,
            self.event_id is not None,
            self.idempotency_key is not None,
        )
        if sum(identities) > 1:
            raise InvalidTrigger("a materialization carries at most one dedupe identity")


@dataclass(frozen=True, slots=True)
class TriggerMaterializationOutcome:
    """What one materialization attempt produced.

    ``duplicate`` is **ephemeral** and is never persisted: occurrence history records materialized
    occurrences, not every delivery attempt. A duplicate resolves to an occurrence that already
    exists, whose status is whatever its original terminal outcome was — it may be ``RUN_CREATED``
    or ``SKIPPED``, and the original outcome is authoritative.
    """

    occurrence: TriggerOccurrence
    duplicate: bool = False


class TriggerMaterializationPersistence(Protocol):
    """Atomic trigger materialization. The implementation owns the transaction."""

    def materialize_occurrence_and_run(
        self, command: TriggerMaterializationCommand
    ) -> TriggerMaterializationOutcome:
        """Materialize one occurrence and, where the trigger permits it, one ordinary Run.

        One ``BEGIN IMMEDIATE`` contains: the trigger re-read, the authority check, the identity
        check against the current kind, an existing-identity lookup, the Run and Job insertion
        through the one canonical helper, the occurrence insert, and the link between them. Either
        the whole attempt commits or nothing happened and the trigger is still due.

        A refusal that ADR 0018 defines as safe — a disabled Agent Instance, an input too large for
        the target — writes a ``skipped`` occurrence in the same transaction instead of inventing a
        Run. Admission backpressure is **not** such a refusal: it rolls the attempt back entirely so
        that no occurrence is lost and the trigger is retried on a later tick.
        """
        ...


class TriggerPersistence(Protocol):
    """Owner-scoped trigger configuration and occurrence history."""

    def create_trigger(
        self, owner_user_id: int, draft: TriggerDraft, now: datetime
    ) -> TriggerDefinition: ...

    def get_trigger(self, owner_user_id: int, trigger_id: int) -> TriggerDefinition: ...

    def list_triggers(
        self, owner_user_id: int, limit: int, before_id: int | None
    ) -> tuple[TriggerDefinition, ...]: ...

    def update_trigger(
        self, owner_user_id: int, trigger_id: int, edit: TriggerEdit, now: datetime
    ) -> TriggerDefinition: ...

    def set_enabled(
        self,
        owner_user_id: int,
        trigger_id: int,
        enabled: bool,
        now: datetime,
        *,
        next_fire_at: datetime | None = None,
    ) -> TriggerDefinition: ...

    def delete_trigger(self, owner_user_id: int, trigger_id: int) -> None: ...

    def list_occurrences(
        self, owner_user_id: int, trigger_id: int, limit: int, before_id: int | None
    ) -> tuple[TriggerOccurrence, ...]: ...

    def occurrence_for_run(self, run_id: int) -> TriggerOccurrence | None: ...


class RunOriginReader(Protocol):
    """Reverse provenance: why a Run exists.

    Descriptive only. No module that claims, starts, retries, recovers, cancels or terminalizes may
    consult this — a descriptive record that can influence execution is not descriptive.
    """

    def occurrence_for_run(self, run_id: int) -> TriggerOccurrence | None: ...


def resolve_agent_definition(
    definitions: AgentDefinitionResolver, trigger: TriggerDefinition
) -> ResolvedAgentDefinition:
    """Resolve a trigger's target Agent through the shared Agent Definition resolver.

    A trigger read without its joined Agent row carries no definition identity, and this refuses
    rather than guessing one. The resolved value is checked against the durable row inside the
    transaction by the canonical helper's existing Agent/definition consistency predicates, so a
    trigger retargeted between the read and the transaction fails closed instead of creating a Run
    against the wrong Agent.

    This is a function rather than a method so that both materialization paths — the E1
    occurrence seam and the scheduler — resolve through one implementation and cannot diverge.
    """
    definition_id = trigger.agent_definition_id
    if definition_id is None:
        raise InvalidTrigger("the trigger read supplied no target agent definition")
    definition = definitions.resolve(definition_id)
    return ResolvedAgentDefinition(definition_id=definition.identity, limits=definition.limits)


class TriggerMaterializationService:
    """Resolve a trigger's target Agent Definition, then hand one command to atomic persistence.

    This is the smallest application seam that keeps Agent Definition resolution identical to a
    manual submission's. The service owns **resolution only**: it reads nothing from the database,
    holds no connection, and makes no authority decision. Everything authoritative happens inside
    the persistence transaction, which re-reads the trigger and uses its owner, target and input.
    """

    def __init__(
        self,
        definitions: AgentDefinitionResolver,
        materializer: TriggerMaterializationPersistence,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._definitions = definitions
        self._materializer = materializer
        self._max_attempts = max_attempts

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    def resolve_definition(self, trigger: TriggerDefinition) -> ResolvedAgentDefinition:
        """Resolve the trigger's target Agent through the shared Agent Definition resolver."""
        return resolve_agent_definition(self._definitions, trigger)

    def materialize(
        self,
        trigger: TriggerDefinition,
        *,
        now: datetime,
        occurred_at: datetime,
        nominal_at: datetime | None = None,
        event_id: str | None = None,
        idempotency_key: str | None = None,
        payload_digest: bytes | None = None,
        payload_bytes: int | None = None,
    ) -> TriggerMaterializationOutcome:
        return self._materializer.materialize_occurrence_and_run(
            TriggerMaterializationCommand(
                trigger_definition_id=trigger.id,
                definition=self.resolve_definition(trigger),
                now=now,
                occurred_at=occurred_at,
                nominal_at=nominal_at,
                event_id=event_id,
                idempotency_key=idempotency_key,
                payload_digest=payload_digest,
                payload_bytes=payload_bytes,
            )
        )


# ------------------------------------------------------------------------------------------------
# The scheduler seam: what a due schedule is, and what one atomic materialization produced
# ------------------------------------------------------------------------------------------------


class StaleReason(StrEnum):
    """Why a due candidate produced nothing at all.

    Every member means the same thing operationally — *the state this decision was computed from
    is no longer the state that exists* — and the distinction is recorded for the operator and for
    the test suite, never acted on differently. A stale candidate writes nothing: no occurrence, no
    Run, no Job, and no schedule mutation. Recording it as an occurrence would turn a harmless race
    into permanent history, and a later tick re-evaluates the current state anyway.
    """

    #: The trigger's configuration changed after the scan.
    REVISION_CHANGED = "revision_changed"
    #: The trigger's next fire time moved after the scan, so this decision is already accounted for.
    NEXT_FIRE_CHANGED = "next_fire_changed"
    #: The trigger was disabled after the scan.
    DISABLED = "disabled"
    #: The trigger was deleted after the scan.
    DELETED = "deleted"
    #: The trigger stopped being a clock-driven schedule.
    NOT_SCHEDULE = "not_schedule"
    #: The trigger's next fire time is no longer due.
    NOT_DUE = "not_due"
    #: The target Agent Instance now resolves to a different Agent Definition.
    AGENT_DEFINITION_CHANGED = "agent_definition_changed"


class ScheduleOutcomeKind(StrEnum):
    """What one scheduler materialization attempt produced. Never persisted.

    Only two of these correspond to a durable row — ``MATERIALIZED`` and ``SKIPPED`` write an
    occurrence. ``DUPLICATED``, ``STALE`` and ``DEFERRED_CAPACITY`` write nothing at all, and exist
    so the tick can say what it did without inventing history to say it with.
    """

    MATERIALIZED = "materialized"
    SKIPPED = "skipped"
    #: The deterministic identity already existed. The recorded outcome is authoritative.
    DUPLICATED = "duplicated"
    #: The decision no longer matches durable state. Nothing was written.
    STALE = "stale"
    #: Admission capacity was full, so the whole attempt rolled back. The schedule is still due.
    DEFERRED_CAPACITY = "deferred_capacity"


@dataclass(frozen=True, slots=True)
class DueScheduleCandidate:
    """One due schedule, together with the durable facts its decision was computed from.

    The two expectations are carried explicitly rather than read back off `trigger` at the moment
    they are needed, because they are the *contract* the transaction verifies: the scan saw this
    revision and this next fire time, and if either has moved the decision must not be applied. A
    scheduler that recomputed them from a fresher read would silently apply a decision to a state
    it was never evaluated against.
    """

    trigger: TriggerDefinition
    expected_config_revision: int
    expected_next_fire_at: datetime


@dataclass(frozen=True, slots=True)
class ScheduleMaterializationCommand:
    """One due schedule's decision, handed to the transaction that must verify and apply it.

    Everything here is either a **precondition to verify** or a **post-state to write**. Nothing
    here is an authority: the transaction re-reads the trigger, and owner, target, revision and
    input come from that row. The two fields that are neither — ``nominal_at`` and
    ``next_fire_at_after`` — are the schedule *decision*, which is the one thing durable state
    cannot supply.

    ``terminal_skip`` is the fatal path: the schedule could not be evaluated at all, so the
    occurrence is recorded as skipped and the trigger is disabled, in the same transaction that
    would otherwise have created a Run. Expressing it as a field of this command rather than as a
    second operation is what keeps "write the skip" and "disable the trigger" from becoming two
    transactions that can disagree.
    """

    trigger_definition_id: int
    definition: ResolvedAgentDefinition
    now: datetime
    occurred_at: datetime
    expected_config_revision: int
    expected_next_fire_at: datetime
    #: The occurrence's identity: the scheduled instant this catch-up stands for.
    nominal_at: datetime
    #: The trigger's next state. `None` completes the schedule, and the transaction disables it.
    next_fire_at_after: datetime | None = None
    #: When set, no Run is created and the occurrence records this static reason instead.
    terminal_skip: SkipReason | None = None

    def __post_init__(self) -> None:
        if self.trigger_definition_id <= 0:
            raise InvalidTrigger("invalid trigger identifier")
        if self.expected_config_revision <= 0:
            raise InvalidTrigger("invalid expected revision")
        moment = _require_aware(self.now)
        occurred = _require_aware(self.occurred_at)
        nominal = _require_aware(self.nominal_at)
        expected = _require_aware(self.expected_next_fire_at)
        object.__setattr__(self, "now", moment)
        object.__setattr__(self, "occurred_at", occurred)
        object.__setattr__(self, "nominal_at", nominal)
        object.__setattr__(self, "expected_next_fire_at", expected)
        if nominal > moment:
            raise InvalidTrigger("a nominal instant cannot be in the future")
        if self.next_fire_at_after is not None:
            following = _require_aware(self.next_fire_at_after)
            object.__setattr__(self, "next_fire_at_after", following)
            if following <= moment:
                raise InvalidTrigger("the next fire time must be strictly in the future")


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidTrigger("a schedule instant must be timezone-aware")
    return value.astimezone(UTC)


#: The outcomes that wrote no durable row, and therefore carry no occurrence.
_OUTCOMES_WITHOUT_AN_OCCURRENCE = (
    ScheduleOutcomeKind.STALE,
    ScheduleOutcomeKind.DEFERRED_CAPACITY,
)


@dataclass(frozen=True, slots=True)
class ScheduleMaterializationOutcome:
    """What one scheduler materialization attempt did, in the vocabulary the tick counts."""

    kind: ScheduleOutcomeKind
    occurrence: TriggerOccurrence | None = None
    stale_reason: StaleReason | None = None

    def __post_init__(self) -> None:
        if self.kind in _OUTCOMES_WITHOUT_AN_OCCURRENCE:
            if self.occurrence is not None:
                raise InvalidTrigger("an outcome that wrote nothing carries no occurrence")
            if self.kind is ScheduleOutcomeKind.STALE and self.stale_reason is None:
                raise InvalidTrigger("a stale outcome carries a reason")
            if self.kind is ScheduleOutcomeKind.DEFERRED_CAPACITY and self.stale_reason is not None:
                raise InvalidTrigger("a deferred outcome carries no stale reason")
        elif self.stale_reason is not None or self.occurrence is None:
            raise InvalidTrigger("an applied outcome carries an occurrence and no reason")


class SchedulerPersistence(Protocol):
    """What the scheduler needs from durable state, and nothing more.

    Two operations, because the scheduler does exactly two things: read which schedules are due,
    and hand one decision to a transaction that verifies and applies it. It never opens a
    transaction itself, never reads an execution table, and holds no capability to claim, retry,
    cancel or terminalize anything.
    """

    def due_schedule_candidates(
        self, *, now: datetime, limit: int, after: tuple[datetime, int] | None
    ) -> tuple[DueScheduleCandidate, ...]:
        """The due schedules, in a stable order, bounded to `limit`, resumed after `after`.

        The ordering is total so that a bounded page can be continued without skipping or
        repeating; `after` is that continuation, and it is scan state only. Correctness never
        depends on it: every candidate is re-verified inside the transaction that acts on it.
        """
        ...

    def materialize_schedule_occurrence(
        self, command: ScheduleMaterializationCommand
    ) -> ScheduleMaterializationOutcome:
        """Verify one decision against durable state and apply it, or write nothing.

        One ``BEGIN IMMEDIATE`` contains the trigger re-read, the existing-identity lookup, the
        precondition checks, the canonical Run and Job insertion, the occurrence insert, and the
        schedule state transition.
        """
        ...


def identity_field_for(kind: TriggerKind) -> str | None:
    """Which dedupe identity a trigger of this kind must use, or None when it has none.

    A keyless webhook delivery has no deterministic identity by design, so it is never deduped —
    two identical payloads can be two legitimately distinct events.
    """
    if kind in (TriggerKind.ONE_TIME, TriggerKind.INTERVAL, TriggerKind.CRON):
        return "nominal_at"
    if kind is TriggerKind.EVENT:
        return "event_id"
    return None


def occurrence_is_materialized(occurrence: TriggerOccurrence) -> bool:
    return occurrence.status is OccurrenceStatus.RUN_CREATED
