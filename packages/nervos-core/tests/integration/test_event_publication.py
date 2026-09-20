"""Stage E4 internal-event publication against a real migrated database.

Everything here drives the composed publication service -- bounded discovery through the trigger
persistence, then the per-candidate materialization -- over a disposable database, so what is
asserted is the durable outcome rather than a fake's opinion of it. The matrix is the
authorization's: fanout bounds, identity conflict, disabled targets, over-large input, capacity,
drift, edits, disables, deletes, and partial retry -- all deterministic, no thread, no sleep.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from nervos_core.application.agent_definitions import create_builtin_definition_registry
from nervos_core.application.events import (
    MAX_EVENT_FANOUT,
    EventEnvelope,
    EventFanoutExceeded,
    EventOutcomeKind,
    EventPublicationResult,
    EventPublicationService,
)
from nervos_core.application.triggers import TriggerDraft
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.events import EventPayload, parse_event_payload
from nervos_core.domain.triggers import TriggerKind
from nervos_core.infrastructure.database.triggers import SqlAlchemyTriggerPersistence
from scheduler_support import AGENT, NOW, OTHER_OWNER, OWNER, migrate
from sqlalchemy import Engine, text

DEFINITION_ID = AgentDefinitionId("nervos.chat", "2")
PAYLOAD = parse_event_payload({"temperature": 21})
OTHER_PAYLOAD = parse_event_payload({"temperature": 22})
EVENT_TYPE = "device.reading"
OTHER_TYPE = "device.other"


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[EventRig]:
    built = EventRig.build(tmp_path / "nervos.db", monkeypatch)
    try:
        yield built
    finally:
        built.engine.dispose()


class EventRig:
    """One migrated database and the composed E4 publication surface."""

    def __init__(self, engine: Engine, path: Path) -> None:
        self.engine = engine
        self.path = path
        self.triggers = SqlAlchemyTriggerPersistence(engine, sleep=lambda _: None)
        self.definitions = create_builtin_definition_registry()
        self.service = EventPublicationService(
            self.definitions, self.triggers, self.triggers, clock=lambda: NOW
        )

    @staticmethod
    def build(path: Path, monkeypatch: pytest.MonkeyPatch) -> EventRig:
        return EventRig(migrate(path, monkeypatch), path)

    # --------------------------------------------------------------------------------------
    # Configuration
    # --------------------------------------------------------------------------------------

    def create(
        self,
        *,
        event_type: str = EVENT_TYPE,
        enabled: bool = True,
        owner: int = OWNER,
        display_name: str = "On reading",
        input_text: str = "watch the sensor",
        agent_instance_id: int = AGENT,
    ):
        """Create one event trigger owned by `owner`."""
        return self.triggers.create_trigger(
            owner,
            TriggerDraft(
                agent_instance_id=agent_instance_id,
                display_name=display_name,
                input_text=input_text,
                kind=TriggerKind.EVENT,
                event_type=event_type,
                enabled=enabled,
            ),
            NOW,
        )

    def publish(
        self,
        *,
        event_id: str = "evt-1",
        owner: int = OWNER,
        event_type: str = EVENT_TYPE,
        payload: EventPayload = PAYLOAD,
        occurred_at: datetime = NOW,
    ) -> EventPublicationResult:
        envelope = EventEnvelope(
            event_id=event_id,
            owner_user_id=owner,
            event_type=event_type,
            payload=payload,
            occurred_at=occurred_at,
        )
        return self.service.publish(envelope)

    def add_other_agent(self) -> None:
        """Give the second owner its own Agent Instance (id 2)."""
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO agent_instances(owner_user_id,agent_key,"
                    "agent_definition_version,display_name,enabled,model_provider,model_name,"
                    "created_at,updated_at) "
                    "VALUES(:owner,'nervos.chat','2','Other Agent',1,'anthropic',"
                    "'opaque/model',:now,:now)"
                ),
                {"owner": OTHER_OWNER, "now": NOW},
            )

    # --------------------------------------------------------------------------------------
    # Durable truth
    # --------------------------------------------------------------------------------------

    def counts(self) -> dict[str, int]:
        with self.engine.connect() as connection:
            return {
                table: int(connection.scalar(text(f"SELECT COUNT(*) FROM {table}")) or 0)
                for table in ("runs", "jobs", "job_attempts", "run_events", "trigger_occurrences")
            }

    def occurrences(self, trigger_id: int) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT id, status, skip_code, run_id, event_id, payload_digest, "
                        "payload_bytes, trigger_revision FROM trigger_occurrences "
                        "WHERE trigger_definition_id = :trigger ORDER BY id"
                    ),
                    {"trigger": trigger_id},
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    def run_input(self, run_id: int) -> str:
        with self.engine.connect() as connection:
            row = connection.execute(
                text("SELECT input_text FROM runs WHERE id = :id"), {"id": run_id}
            ).scalar_one()
        return str(row)


# ------------------------------------------------------------------------------------------------
# The happy path: one match creates one ordinary Run, Job and occurrence
# ------------------------------------------------------------------------------------------------


def test_one_exact_match_materializes_one_occurrence_run_and_job(rig: EventRig) -> None:
    trigger = rig.create()

    result = rig.publish()

    assert result.matched_count == 1
    assert result.run_created_count == 1
    assert result.complete is True
    outcome = result.outcomes[0]
    assert outcome.kind is EventOutcomeKind.RUN_CREATED
    assert outcome.trigger_id == trigger.id
    assert outcome.occurrence is not None
    assert rig.counts() == {
        "runs": 1,
        "jobs": 1,
        "job_attempts": 0,
        "run_events": 2,
        "trigger_occurrences": 1,
    }

    rows = rig.occurrences(trigger.id)
    assert len(rows) == 1
    assert rows[0]["status"] == "run_created"
    assert rows[0]["run_id"] == outcome.occurrence.run_id
    assert rows[0]["event_id"] == "evt-1"
    assert rows[0]["payload_bytes"] == PAYLOAD.byte_count
    assert bytes(rows[0]["payload_digest"]) == PAYLOAD.digest


def test_the_run_is_an_ordinary_run_with_the_frozen_event_envelope(rig: EventRig) -> None:
    rig.create(input_text="Explain the reading.")

    result = rig.publish()
    assert result.run_created_count == 1
    occurrence = result.outcomes[0].occurrence
    assert occurrence is not None
    run_id = occurrence.run_id
    assert run_id is not None

    composed = rig.run_input(run_id)
    # canonical_json_text sorts keys recursively: payload precedes type inside the envelope.
    assert (
        composed == '{"instruction":"Explain the reading.",'
        '"untrusted_event":{"payload":{"temperature":21},"type":"device.reading"}}'
    )


def test_zero_matches_write_nothing(rig: EventRig) -> None:
    rig.create(event_type=OTHER_TYPE)

    result = rig.publish(event_type="device.unknown")

    assert result.matched_count == 0
    assert result.outcomes == ()
    assert result.complete is True
    assert rig.counts() == {
        "runs": 0,
        "jobs": 0,
        "job_attempts": 0,
        "run_events": 0,
        "trigger_occurrences": 0,
    }


def test_thirty_two_matches_all_materialize(rig: EventRig) -> None:
    for index in range(MAX_EVENT_FANOUT):
        rig.create(display_name=f"Sensor {index}")

    result = rig.publish()

    assert result.matched_count == MAX_EVENT_FANOUT
    assert result.run_created_count == MAX_EVENT_FANOUT
    assert [outcome.trigger_id for outcome in result.outcomes] == list(range(1, 33))
    assert rig.counts()["runs"] == MAX_EVENT_FANOUT
    assert rig.counts()["trigger_occurrences"] == MAX_EVENT_FANOUT


def test_thirty_three_matches_fail_closed_with_zero_writes(rig: EventRig) -> None:
    for index in range(MAX_EVENT_FANOUT + 1):
        rig.create(display_name=f"Sensor {index}")

    with pytest.raises(EventFanoutExceeded):
        rig.publish()

    assert rig.counts() == {
        "runs": 0,
        "jobs": 0,
        "job_attempts": 0,
        "run_events": 0,
        "trigger_occurrences": 0,
    }


# ------------------------------------------------------------------------------------------------
# Identity: duplicates answer, conflicts refuse, other kinds never dedupe
# ------------------------------------------------------------------------------------------------


def test_a_same_id_same_payload_publication_is_a_duplicate(rig: EventRig) -> None:
    trigger = rig.create()
    first = rig.publish()
    assert first.run_created_count == 1

    second = rig.publish()

    assert second.duplicate_count == 1
    assert second.run_created_count == 0
    assert rig.counts()["runs"] == 1
    assert len(rig.occurrences(trigger.id)) == 1


def test_a_same_id_different_payload_publication_is_a_conflict(rig: EventRig) -> None:
    trigger = rig.create()
    assert rig.publish().run_created_count == 1

    result = rig.publish(payload=OTHER_PAYLOAD)

    assert result.event_id_conflict_count == 1
    assert result.run_created_count == 0
    assert result.duplicate_count == 0
    assert rig.counts()["runs"] == 1
    assert len(rig.occurrences(trigger.id)) == 1


def test_a_skipped_identity_is_also_a_duplicate_on_retry(rig: EventRig) -> None:
    trigger = rig.create()
    with rig.engine.begin() as connection:
        connection.execute(
            text("UPDATE agent_instances SET enabled = 0 WHERE id = :id"), {"id": AGENT}
        )
    first = rig.publish()
    assert first.skipped_count == 1

    second = rig.publish()

    assert second.duplicate_count == 1
    assert rig.counts()["runs"] == 0
    assert len(rig.occurrences(trigger.id)) == 1


# ------------------------------------------------------------------------------------------------
# Disabled, edited, deleted, drifted, and overlarge targets
# ------------------------------------------------------------------------------------------------


def test_a_disabled_trigger_is_not_discovered(rig: EventRig) -> None:
    rig.create(enabled=False)

    result = rig.publish()

    assert result.matched_count == 0
    assert result.outcomes == ()
    assert rig.counts()["trigger_occurrences"] == 0


def test_a_disabled_trigger_that_already_committed_answers_duplicate(rig: EventRig) -> None:
    trigger = rig.create()
    assert rig.publish().run_created_count == 1
    rig.triggers.set_enabled(OWNER, trigger.id, False, NOW)

    result = rig.publish()

    assert result.matched_count == 0
    assert result.outcomes == ()
    assert len(rig.occurrences(trigger.id)) == 1


def test_an_event_type_edit_away_removes_the_match(rig: EventRig) -> None:
    from nervos_core.application.triggers import TriggerEdit

    trigger = rig.create()
    rig.triggers.update_trigger(
        OWNER,
        trigger.id,
        TriggerEdit(display_name="On reading", input_text="watch", event_type=OTHER_TYPE),
        NOW,
    )

    # The trigger no longer matches this event type, so discovery finds nothing: the edit won.
    result = rig.publish()
    assert result.matched_count == 0
    assert rig.counts()["trigger_occurrences"] == 0


def test_a_deleted_trigger_produces_no_ghost_run(rig: EventRig) -> None:
    trigger = rig.create()
    rig.triggers.delete_trigger(OWNER, trigger.id)

    result = rig.publish()

    assert result.matched_count == 0
    assert rig.counts()["runs"] == 0


def test_an_agent_disabled_target_consumes_the_identity_as_a_skip(rig: EventRig) -> None:
    trigger = rig.create()
    with rig.engine.begin() as connection:
        connection.execute(
            text("UPDATE agent_instances SET enabled = 0 WHERE id = :id"), {"id": AGENT}
        )

    result = rig.publish()

    assert result.skipped_count == 1
    row = rig.occurrences(trigger.id)[0]
    assert row["status"] == "skipped"
    assert row["skip_code"] == "agent_disabled"
    assert row["run_id"] is None
    # The event genuinely arrived and was evaluated, so its metadata explains the skip.
    assert row["payload_bytes"] == PAYLOAD.byte_count
    assert bytes(row["payload_digest"]) == PAYLOAD.digest


def test_an_overlarge_composed_input_is_a_skip_not_a_truncation(rig: EventRig) -> None:
    trigger = rig.create(input_text="i" * 3900)
    big = parse_event_payload({"blob": "x" * 4000})

    result = rig.publish(payload=big)

    assert result.skipped_count == 1
    row = rig.occurrences(trigger.id)[0]
    assert row["status"] == "skipped"
    assert row["skip_code"] == "input_too_large"
    assert rig.counts()["runs"] == 0


def test_a_drifted_agent_definition_is_retryable_without_consuming_the_identity(
    rig: EventRig,
) -> None:
    rig.create()
    with rig.engine.begin() as connection:
        connection.execute(
            text("UPDATE agent_instances SET agent_definition_version = '99' WHERE id = :id"),
            {"id": AGENT},
        )

    result = rig.publish()

    assert result.stale_count == 1
    assert rig.counts()["trigger_occurrences"] == 0
    assert rig.counts()["runs"] == 0


# ------------------------------------------------------------------------------------------------
# Capacity, partial fanout, retry, and isolation
# ------------------------------------------------------------------------------------------------


def test_queue_capacity_rolls_back_only_its_own_candidate(rig: EventRig) -> None:
    from nervos_core.application.events import EventEnvelope
    from nervos_core.domain.runs import TOOL_ENABLED_LIMITS
    from nervos_core.infrastructure.database.jobs import SqlAlchemyJobPersistence

    first = rig.create(display_name="First")
    second = rig.create(display_name="Second")

    filler = SqlAlchemyJobPersistence(rig.engine, max_pending=1)
    filler.submit(
        owner_user_id=OWNER,
        agent_instance_id=AGENT,
        input_text="occupy the queue",
        limits=TOOL_ENABLED_LIMITS,
        definition_id=DEFINITION_ID,
        now=NOW,
    )
    limited = SqlAlchemyTriggerPersistence(rig.engine, max_pending=1)
    service = EventPublicationService(rig.definitions, limited, limited, clock=lambda: NOW)

    result = service.publish(
        EventEnvelope(
            event_id="evt-cap",
            owner_user_id=OWNER,
            event_type=EVENT_TYPE,
            payload=PAYLOAD,
            occurred_at=NOW,
        )
    )

    assert result.capacity_deferred_count == 2
    assert result.run_created_count == 0
    assert rig.occurrences(first.id) == []
    assert rig.occurrences(second.id) == []
    # The filler Run is the only durable work: both candidates rolled back whole.
    with rig.engine.connect() as connection:
        runs = connection.scalar(text("SELECT COUNT(*) FROM runs"))
    assert int(runs) == 1


def test_a_retry_after_partial_fanout_fills_only_the_missing_identities(rig: EventRig) -> None:
    from nervos_core.application.events import EventEnvelope

    first = rig.create(display_name="First")
    second = rig.create(display_name="Second")
    service = EventPublicationService(
        rig.definitions, rig.triggers, rig.triggers, clock=lambda: NOW
    )
    envelope = EventEnvelope(
        event_id="evt-partial",
        owner_user_id=OWNER,
        event_type=EVENT_TYPE,
        payload=PAYLOAD,
        occurred_at=NOW,
    )

    one = service.publish(envelope)
    assert one.run_created_count == 2

    # A later matching trigger joins on the same event id: it may materialize for that trigger,
    # while the committed identities answer duplicate.
    third = rig.create(display_name="Third")
    two = service.publish(envelope)

    kinds = {outcome.trigger_id: outcome.kind for outcome in two.outcomes}
    assert kinds[first.id] is EventOutcomeKind.DUPLICATE
    assert kinds[second.id] is EventOutcomeKind.DUPLICATE
    assert kinds[third.id] is EventOutcomeKind.RUN_CREATED
    assert two.run_created_count == 1
    assert two.duplicate_count == 2


def test_cross_owner_triggers_never_fan_out_to_another_owner(rig: EventRig) -> None:
    rig.add_other_agent()
    rig.create(owner=OTHER_OWNER, agent_instance_id=2)

    result = rig.publish()

    assert result.matched_count == 0
    assert rig.counts()["trigger_occurrences"] == 0


def test_no_event_record_table_exists(rig: EventRig) -> None:
    """There is no event ledger: an event exists durably only per trigger occurrence."""
    from sqlalchemy import inspect as sa_inspect

    rig.create()
    rig.publish(event_type="device.unknown")

    tables = set(sa_inspect(rig.engine).get_table_names())
    assert not any("event_record" in name or name == "events" for name in tables)
