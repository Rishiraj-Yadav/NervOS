"""Stage E4 internal events: publication, exact-match fanout, and per-trigger materialization.

This module is provider-neutral and transport-neutral: no FastAPI, SQLAlchemy, HTTP or database
handle appears here. An event is data, not authority, and there is no public HTTP ingress for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from nervos_core.application.agent_definitions import (
    AgentDefinitionResolver,
    BuiltInAgentDefinitionRegistry,
    UnknownAgentDefinition,
)
from nervos_core.application.clock import Clock, require_utc
from nervos_core.application.errors import PersistenceUnavailable, QueueCapacityExceeded
from nervos_core.application.triggers import (
    ResolvedAgentDefinition,
    resolve_agent_definition,
)
from nervos_core.domain.events import EventPayload, validate_event_id
from nervos_core.domain.triggers import (
    InvalidTrigger,
    TriggerDefinition,
    TriggerOccurrence,
    validate_event_type,
)

#: The hard fanout bound. Discovery reads one row past it so an over-wide event is provable.
MAX_EVENT_FANOUT = 32


class EventFanoutExceeded(ValueError):
    """The event matched more than the frozen fanout bound; the whole publication is refused."""


class EventDefinitionStale(ValueError):
    """The trigger's authority moved between discovery and materialization.

    Typed, never message-parsed: the trigger was edited, disabled or retargeted, so the candidate
    consumes nothing and is retryable against the current configuration, not the discovered one.
    """


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """One internal event, provider-neutral and immutable.

    ``owner_user_id`` is the internal publisher's authority, never an HTTP body field. The payload
    is already validated and canonicalized. Nothing here names an Agent, trigger, Run, provider,
    model, grant or tool: the envelope is *what happened*; the trigger rows decide *who reacts*.
    """

    event_id: str
    owner_user_id: int
    event_type: str
    payload: EventPayload
    occurred_at: datetime

    def __post_init__(self) -> None:
        validate_event_id(self.event_id)
        if self.owner_user_id <= 0:
            raise InvalidTrigger("invalid event owner")
        validate_event_type(self.event_type)
        try:
            object.__setattr__(self, "occurred_at", require_utc(self.occurred_at))
        except ValueError as error:
            raise InvalidTrigger("an event instant must be timezone-aware") from error


@dataclass(frozen=True, slots=True)
class EventMaterializationCommand:
    """Everything one event materialization must verify or write, and nothing more.

    The envelope supplies the event's identity and payload; ``definition`` is the resolved Agent
    Definition for the trigger's target. Owner, enabled state, event type, target and instruction
    are all re-read from the durable row inside the transaction, so the command carries no stale
    authority.
    """

    trigger_definition_id: int
    envelope: EventEnvelope
    definition: ResolvedAgentDefinition
    now: datetime

    def __post_init__(self) -> None:
        if self.trigger_definition_id <= 0:
            raise InvalidTrigger("invalid trigger identifier")
        object.__setattr__(self, "now", require_utc(self.now))


class EventOutcomeKind(StrEnum):
    """The terminal per-trigger outcome of one event publication.

    ``RUN_CREATED`` and ``SKIPPED`` wrote an occurrence. ``DUPLICATE`` names an occurrence that
    already existed and wrote nothing new. ``STALE`` and ``CAPACITY_DEFERRED`` wrote nothing and
    are retryable against the current configuration. ``EVENT_ID_CONFLICT`` wrote nothing and is a
    caller-contract violation, not a capacity failure.
    """

    RUN_CREATED = "run_created"
    SKIPPED = "skipped"
    DUPLICATE = "duplicate"
    STALE = "stale"
    CAPACITY_DEFERRED = "capacity_deferred"
    EVENT_ID_CONFLICT = "event_id_conflict"


@dataclass(frozen=True, slots=True)
class EventOccurrenceOutcome:
    """What one trigger's event materialization produced, in the publication's vocabulary."""

    trigger_id: int
    kind: EventOutcomeKind
    occurrence: TriggerOccurrence | None = None


@dataclass(frozen=True, slots=True)
class EventPublicationResult:
    """The immutable, safe account of one publication.

    Carries the event identity, the bounded match count, the per-trigger outcomes and the aggregate
    counts. It exposes no payload, instruction, secret, provider detail or internal exception.
    ``complete`` is ``False`` exactly when a persistence-level failure stopped the publication
    early: what already committed stands, and the caller may retry the same envelope.
    """

    event_id: str
    matched_count: int
    outcomes: tuple[EventOccurrenceOutcome, ...]
    complete: bool = True

    @property
    def run_created_count(self) -> int:
        return self._count(EventOutcomeKind.RUN_CREATED)

    @property
    def skipped_count(self) -> int:
        return self._count(EventOutcomeKind.SKIPPED)

    @property
    def duplicate_count(self) -> int:
        return self._count(EventOutcomeKind.DUPLICATE)

    @property
    def stale_count(self) -> int:
        return self._count(EventOutcomeKind.STALE)

    @property
    def capacity_deferred_count(self) -> int:
        return self._count(EventOutcomeKind.CAPACITY_DEFERRED)

    @property
    def event_id_conflict_count(self) -> int:
        return self._count(EventOutcomeKind.EVENT_ID_CONFLICT)

    def _count(self, kind: EventOutcomeKind) -> int:
        return sum(1 for outcome in self.outcomes if outcome.kind is kind)


class EventDiscoveryPersistence(Protocol):
    """The exact-match read a publication begins with, and nothing more."""

    def list_event_matches(
        self, *, owner_user_id: int, event_type: str, limit: int
    ) -> tuple[TriggerDefinition, ...]:
        """The currently enabled event triggers owned by this owner and matching this type.

        The implementation answers the *current* durable set, ordered by trigger id ascending and
        bounded to ``limit``, so a fanout wider than the frozen maximum is provable before any
        write happens.
        """
        ...


class EventPublicationPersistence(Protocol):
    """The one event materialization a publication performs per candidate, and nothing more."""

    def materialize_event_occurrence(
        self, command: EventMaterializationCommand
    ) -> EventOccurrenceOutcome:
        """Materialize one occurrence for one candidate, in one short transaction.

        The implementation re-reads the trigger, resolves the existing-identity lookup before any
        fresh authority, and applies the current durable state. It returns the outcome and never
        lets a capacity refusal or a stale definition be mistaken for a skip.
        """
        ...


class EventPublicationService:
    """Publish one internal event to its exact-match triggers, one transaction each.

    The service owns the *shape* of a publication -- the fanout bound, the per-candidate isolation,
    and the stop-on-persistence-failure rule -- and nothing durable. Every authoritative decision
    is made inside the persistence transaction, which re-reads the trigger row.
    """

    def __init__(
        self,
        definitions: AgentDefinitionResolver,
        persistence: EventDiscoveryPersistence,
        materializer: EventPublicationPersistence,
        clock: Clock,
    ) -> None:
        self._definitions = definitions
        self._persistence = persistence
        self._materializer = materializer
        self._clock = clock

    def publish(self, envelope: EventEnvelope) -> EventPublicationResult:
        """Publish one event, failing closed on an over-wide fanout before any write."""
        now = require_utc(self._clock())
        candidates = self._persistence.list_event_matches(
            owner_user_id=envelope.owner_user_id,
            event_type=envelope.event_type,
            limit=MAX_EVENT_FANOUT + 1,
        )
        if len(candidates) > MAX_EVENT_FANOUT:
            raise EventFanoutExceeded(
                f"event matched more than {MAX_EVENT_FANOUT} triggers; nothing was published"
            )

        outcomes: list[EventOccurrenceOutcome] = []
        complete = True
        for candidate in candidates:
            try:
                definition = resolve_agent_definition(self._definitions, candidate)
            except UnknownAgentDefinition:
                # A built-in registry miss means the durable target drifted after discovery and is
                # retryable. Other resolver failures retain the manual-submission contract.
                if not isinstance(self._definitions, BuiltInAgentDefinitionRegistry):
                    raise
                outcomes.append(
                    EventOccurrenceOutcome(trigger_id=candidate.id, kind=EventOutcomeKind.STALE)
                )
                continue
            except InvalidTrigger:
                # A malformed or retargeted trigger is candidate-local and fails closed.
                outcomes.append(
                    EventOccurrenceOutcome(trigger_id=candidate.id, kind=EventOutcomeKind.STALE)
                )
                continue
            command = EventMaterializationCommand(
                trigger_definition_id=candidate.id,
                envelope=envelope,
                definition=definition,
                now=now,
            )
            try:
                outcome = self._materializer.materialize_event_occurrence(command)
            except QueueCapacityExceeded:
                # Candidate-local: the trigger's own admission rolled back, so its identity stays
                # unconsumed and retryable. Independent candidates continue.
                outcomes.append(
                    EventOccurrenceOutcome(
                        trigger_id=candidate.id, kind=EventOutcomeKind.CAPACITY_DEFERRED
                    )
                )
                continue
            except EventDefinitionStale:
                # The trigger moved between discovery and the transaction. Nothing was written.
                outcomes.append(
                    EventOccurrenceOutcome(trigger_id=candidate.id, kind=EventOutcomeKind.STALE)
                )
                continue
            except PersistenceUnavailable:
                # A database-level failure is not a per-trigger outcome: stop now, keep what
                # already committed, and let the caller retry the same envelope.
                complete = False
                break
            outcomes.append(outcome)

        return EventPublicationResult(
            event_id=envelope.event_id,
            matched_count=len(candidates),
            outcomes=tuple(outcomes),
            complete=complete,
        )


__all__ = [
    "MAX_EVENT_FANOUT",
    "EventDefinitionStale",
    "EventDiscoveryPersistence",
    "EventEnvelope",
    "EventFanoutExceeded",
    "EventMaterializationCommand",
    "EventOccurrenceOutcome",
    "EventOutcomeKind",
    "EventPublicationPersistence",
    "EventPublicationResult",
    "EventPublicationService",
]
