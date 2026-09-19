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
from datetime import datetime
from typing import Protocol

from nervos_core.application.agent_definitions import AgentDefinitionResolver
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.runs import RunLimits
from nervos_core.domain.triggers import (
    InvalidTrigger,
    MisfirePolicy,
    OccurrenceStatus,
    ScheduleSpec,
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
        """Resolve the trigger's target Agent through the shared Agent Definition resolver.

        A trigger read without its joined Agent row carries no definition identity, and this refuses
        rather than guessing one. The resolved value is checked against the durable row inside the
        transaction by the canonical helper's existing Agent/definition consistency predicates, so a
        trigger retargeted between the read and the transaction fails closed instead of creating a
        Run against the wrong Agent.
        """
        definition_id = trigger.agent_definition_id
        if definition_id is None:
            raise InvalidTrigger("the trigger read supplied no target agent definition")
        definition = self._definitions.resolve(definition_id)
        return ResolvedAgentDefinition(definition_id=definition.identity, limits=definition.limits)

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
