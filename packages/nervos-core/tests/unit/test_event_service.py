"""Stage E4 internal-event publication: envelope, fanout bound, retry semantics, and results.

The service is exercised against recording fakes rather than a database, because what is under
test here is the *order* of its decisions -- fail closed on over-wide fanout before any write,
then per-candidate isolation, then stop on a persistence failure -- and an order is easier to
assert against a list of calls than against durable side effects.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from nervos_core.application.agent_definitions import (
    AgentDefinitionResolver,
    UnknownAgentDefinition,
)
from nervos_core.application.errors import (
    PersistenceContention,
    QueueCapacityExceeded,
)
from nervos_core.application.events import (
    MAX_EVENT_FANOUT,
    EventEnvelope,
    EventFanoutExceeded,
    EventMaterializationCommand,
    EventOccurrenceOutcome,
    EventOutcomeKind,
    EventPublicationResult,
    EventPublicationService,
)
from nervos_core.domain.agents import AgentDefinition, AgentDefinitionId
from nervos_core.domain.events import (
    EventPayload,
    InvalidEventPayload,
    parse_event_payload,
)
from nervos_core.domain.runs import TOOL_ENABLED_LIMITS
from nervos_core.domain.triggers import (
    InvalidTrigger,
    MisfirePolicy,
    OccurrenceStatus,
    TriggerDefinition,
    TriggerKind,
    TriggerOccurrence,
)

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
OWNER = 1
DEFINITION_ID = AgentDefinitionId("nervos.chat", "2")
PAYLOAD = parse_event_payload({"temperature": 21})


def envelope(
    *,
    event_id: str = "evt-1",
    owner: int = OWNER,
    event_type: str = "device.reading",
    payload: EventPayload = PAYLOAD,
    occurred_at: datetime = NOW,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        owner_user_id=owner,
        event_type=event_type,
        payload=payload,
        occurred_at=occurred_at,
    )


def event_definition(
    *,
    trigger_id: int = 1,
    owner: int = OWNER,
    enabled: bool = True,
    event_type: str = "device.reading",
) -> TriggerDefinition:
    return TriggerDefinition(
        id=trigger_id,
        owner_user_id=owner,
        agent_instance_id=7,
        kind=TriggerKind.EVENT,
        display_name="On reading",
        enabled=enabled,
        input_text="watch the sensor",
        config_revision=1,
        misfire_policy=MisfirePolicy.COALESCE_ONE,
        next_fire_at=None,
        run_at=None,
        interval_seconds=None,
        cron_expression=None,
        timezone=None,
        public_id=None,
        created_at=NOW,
        updated_at=NOW,
        event_type=event_type,
        agent_definition_id=DEFINITION_ID,
    )


def occurrence(*, trigger_id: int = 1, run_id: int | None = 3) -> TriggerOccurrence:
    skipped = run_id is None
    return TriggerOccurrence(
        id=trigger_id * 100,
        trigger_definition_id=trigger_id,
        owner_user_id=OWNER,
        agent_instance_id=7,
        trigger_revision=1,
        status=OccurrenceStatus.SKIPPED if skipped else OccurrenceStatus.RUN_CREATED,
        run_id=run_id,
        skip_code="input_too_large" if skipped else None,
        skip_message="static" if skipped else None,
        nominal_at=None,
        event_id="evt-1",
        idempotency_key=None,
        payload_digest=PAYLOAD.digest,
        payload_bytes=PAYLOAD.byte_count,
        occurred_at=NOW,
        created_at=NOW,
    )


class StaticResolver:
    """Every definition identity resolves to the built-in chat definition."""

    def resolve(self, definition_id: AgentDefinitionId) -> AgentDefinition:
        return AgentDefinition(
            identity=DEFINITION_ID, display_name="NervOS Chat", limits=TOOL_ENABLED_LIMITS
        )


class FakePersistence:
    """One ordered call log across discovery and materialization, with scriptable failures."""

    def __init__(
        self,
        matches: list[TriggerDefinition] | None = None,
        *,
        capacity_on: set[int] | None = None,
        stale_on: set[int] | None = None,
        unavailable_on: set[int] | None = None,
        outcome: EventOutcomeKind = EventOutcomeKind.RUN_CREATED,
    ) -> None:
        self.matches = matches if matches is not None else [event_definition()]
        self.capacity_on = capacity_on or set()
        self.stale_on = stale_on or set()
        self.unavailable_on = unavailable_on or set()
        self.outcome = outcome
        self.discovery_calls: list[tuple[int, str, int]] = []
        self.materialized: list[EventMaterializationCommand] = []

    def list_event_matches(
        self, *, owner_user_id: int, event_type: str, limit: int
    ) -> tuple[TriggerDefinition, ...]:
        self.discovery_calls.append((owner_user_id, event_type, limit))
        return tuple(self.matches[:limit])

    def materialize_event_occurrence(
        self, command: EventMaterializationCommand
    ) -> EventOccurrenceOutcome:
        from nervos_core.application.events import EventDefinitionStale

        self.materialized.append(command)
        trigger_id = command.trigger_definition_id
        if trigger_id in self.capacity_on:
            raise QueueCapacityExceeded
        if trigger_id in self.stale_on:
            raise EventDefinitionStale("moved")
        if trigger_id in self.unavailable_on:
            raise PersistenceContention
        row = occurrence(trigger_id=trigger_id, run_id=None if self._skipped() else 3)
        return EventOccurrenceOutcome(trigger_id=trigger_id, kind=self.outcome, occurrence=row)

    def _skipped(self) -> bool:
        return self.outcome is EventOutcomeKind.SKIPPED


def publish(
    fake: FakePersistence,
    env: EventEnvelope | None = None,
    now: datetime = NOW,
) -> EventPublicationResult:
    service = EventPublicationService(StaticResolver(), fake, fake, clock=lambda: now)
    return service.publish(env if env is not None else envelope())


# ------------------------------------------------------------------------------------------------
# The fanout bound is validated before anything materializes
# ------------------------------------------------------------------------------------------------


def test_discovery_asks_for_one_row_past_the_bound() -> None:
    fake = FakePersistence()
    publish(fake)

    assert fake.discovery_calls == [(OWNER, "device.reading", MAX_EVENT_FANOUT + 1)]


def test_zero_matches_produce_an_empty_complete_result() -> None:
    result = publish(FakePersistence(matches=[]))

    assert result.event_id == "evt-1"
    assert result.matched_count == 0
    assert result.outcomes == ()
    assert result.complete is True
    assert result.run_created_count == 0


def test_thirty_three_matches_fail_closed_before_any_materialization() -> None:
    fake = FakePersistence([event_definition(trigger_id=index) for index in range(1, 34)])
    with pytest.raises(EventFanoutExceeded):
        publish(fake)

    assert fake.materialized == []


def test_thirty_two_matches_all_materialize_in_discovery_order() -> None:
    fake = FakePersistence([event_definition(trigger_id=index) for index in range(1, 33)])
    result = publish(fake)

    assert result.matched_count == 32
    assert [outcome.trigger_id for outcome in result.outcomes] == list(range(1, 33))
    assert result.run_created_count == 32
    assert result.complete is True


def test_the_fanout_bound_is_exactly_thirty_two() -> None:
    assert MAX_EVENT_FANOUT == 32


# ------------------------------------------------------------------------------------------------
# The envelope carries no authority and validates its identity
# ------------------------------------------------------------------------------------------------


def test_an_envelope_refuses_a_cross_owner_shape() -> None:
    with pytest.raises(InvalidTrigger):
        envelope(owner=0)


def test_an_envelope_refuses_an_empty_event_id() -> None:
    with pytest.raises(InvalidTrigger):
        envelope(event_id="")


def test_an_envelope_rejects_the_reserved_namespace() -> None:
    with pytest.raises(InvalidTrigger):
        envelope(event_type="nervos.system")


def test_an_envelope_refuses_a_naive_instant() -> None:
    with pytest.raises(InvalidTrigger):
        envelope(occurred_at=datetime(2026, 9, 20, 12, 0))


def test_an_oversized_payload_is_refused_before_discovery() -> None:
    fake = FakePersistence()
    oversized = "x" * 65_536
    with pytest.raises(InvalidEventPayload):
        parse_event_payload({"text": oversized})

    assert fake.discovery_calls == []


# ------------------------------------------------------------------------------------------------
# Per-trigger outcomes, and what survives a partial fanout
# ------------------------------------------------------------------------------------------------


def test_capacity_is_candidate_local_and_retryable() -> None:
    fake = FakePersistence(
        [event_definition(trigger_id=1), event_definition(trigger_id=2)], capacity_on={1}
    )
    result = publish(fake)

    assert [outcome.kind for outcome in result.outcomes] == [
        EventOutcomeKind.CAPACITY_DEFERRED,
        EventOutcomeKind.RUN_CREATED,
    ]
    assert result.capacity_deferred_count == 1
    assert result.run_created_count == 1
    assert result.complete is True


def test_a_stale_candidate_consumes_nothing_and_continues() -> None:
    fake = FakePersistence(
        [event_definition(trigger_id=1), event_definition(trigger_id=2)], stale_on={1}
    )
    result = publish(fake)

    assert [outcome.kind for outcome in result.outcomes] == [
        EventOutcomeKind.STALE,
        EventOutcomeKind.RUN_CREATED,
    ]
    assert result.stale_count == 1
    assert result.complete is True


def test_a_persistence_failure_stops_later_candidates_and_marks_incomplete() -> None:
    fake = FakePersistence(
        [event_definition(trigger_id=1), event_definition(trigger_id=2)], unavailable_on={1}
    )
    result = publish(fake)

    assert result.outcomes == ()
    assert result.complete is False
    assert [command.trigger_definition_id for command in fake.materialized] == [1]


def test_already_committed_work_stands_when_a_later_candidate_fails() -> None:
    fake = FakePersistence(
        [event_definition(trigger_id=1), event_definition(trigger_id=2)], unavailable_on={2}
    )
    result = publish(fake)

    assert [outcome.kind for outcome in result.outcomes] == [EventOutcomeKind.RUN_CREATED]
    assert result.complete is False


def test_skipped_outcomes_carry_the_canonical_metadata() -> None:
    skipped = FakePersistence([event_definition(trigger_id=5)], outcome=EventOutcomeKind.SKIPPED)
    result = publish(skipped)
    outcome = result.outcomes[0]

    assert outcome.kind is EventOutcomeKind.SKIPPED
    assert outcome.occurrence is not None
    assert outcome.occurrence.payload_digest == PAYLOAD.digest
    assert result.skipped_count == 1
    assert result.run_created_count == 0


def test_the_result_names_no_payload_instruction_or_secret() -> None:
    result = publish(FakePersistence())

    assert "temperature" not in repr(result)
    assert "watch the sensor" not in repr(result)


def test_a_command_is_resolved_against_the_same_registry_a_manual_run_uses() -> None:
    class RefusingResolver(AgentDefinitionResolver):
        def resolve(self, definition_id: AgentDefinitionId) -> AgentDefinition:
            raise UnknownAgentDefinition

    fake = FakePersistence()
    service = EventPublicationService(RefusingResolver(), fake, fake, clock=lambda: NOW)
    with pytest.raises(UnknownAgentDefinition):
        service.publish(envelope())

    assert fake.materialized == []
