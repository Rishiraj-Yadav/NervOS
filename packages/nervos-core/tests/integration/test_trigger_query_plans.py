"""Stage E4 query plans: the six hot-path reads are index seeks over `0008`'s own indexes.

E4 adds no index and no migration. `0008` already carries every access path the management surface
and the internal-event seam need, and these tests prove the *real* composed methods use them rather
than asserting a hand-written query that merely resembles what the code does: each statement is
captured from the engine while the production composition runs, and the captured statement is then
planned against a populated database.

The six reads are the ones that run on every list, every detail view, every history page and every
publication. All six are bound by owner, by kind-shaped identity, or by a unique locator, so none of
them may degenerate into a scan of a table that grows with the owner's data.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from nervos_core.application.agent_definitions import create_builtin_definition_registry
from nervos_core.application.events import EventEnvelope, EventPublicationService
from nervos_core.application.triggers import TriggerDraft
from nervos_core.domain.events import EventPayload, parse_event_payload
from nervos_core.domain.triggers import ScheduleSpec, TriggerKind
from nervos_core.infrastructure.database.triggers import SqlAlchemyTriggerPersistence
from nervos_core.infrastructure.security.webhook_secrets import (
    digest_secret,
    generate_public_id,
    generate_secret,
)
from scheduler_support import AGENT, NOW, OWNER, migrate
from sqlalchemy import Engine, event, text

EVENT_TYPE = "device.reading"
OTHER_TYPE = "device.other"
PAYLOAD = parse_event_payload({"temperature": 21})


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[PlanRig]:
    built = PlanRig.build(tmp_path / "plans.db", monkeypatch)
    try:
        yield built
    finally:
        built.engine.dispose()


class Captured:
    """Every statement an engine executed between `enter` and `exit`, with its parameters."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self.statements: list[tuple[str, Any]] = []

    def __enter__(self) -> Captured:
        def record(
            conn: Any,
            cursor: Any,
            statement: str,
            parameters: Any,
            context: Any,
            executemany: bool,
        ) -> None:
            del conn, cursor, context, executemany
            self.statements.append((statement, parameters))

        self._handler = record
        event.listen(self._engine, "before_cursor_execute", record)
        return self

    def __exit__(self, *_: object) -> None:
        event.remove(self._engine, "before_cursor_execute", self._handler)

    def selects(self, *needles: str) -> list[tuple[str, Any]]:
        return [
            (statement, parameters)
            for statement, parameters in self.statements
            if statement.lstrip().upper().startswith("SELECT")
            and all(needle in statement for needle in needles)
        ]

    def one(self, *needles: str) -> tuple[str, Any]:
        found = self.selects(*needles)
        assert len(found) == 1, f"expected one SELECT for {needles}: {[s for s, _ in found]}"
        return found[0]


def plan_for(engine: Engine, captured: tuple[str, Any]) -> str:
    """Plan the statement the engine actually ran, with its own bindings supplied."""
    statement, parameters = captured
    with engine.connect() as connection:
        rows = connection.exec_driver_sql(f"EXPLAIN QUERY PLAN {statement}", parameters).all()
    return " | ".join(str(row[-1]) for row in rows)


class PlanRig:
    """One migrated database and the composed E4 read/write surface over it."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.triggers = SqlAlchemyTriggerPersistence(engine, sleep=lambda _: None)
        self.definitions = create_builtin_definition_registry()
        self.service = EventPublicationService(
            self.definitions, self.triggers, self.triggers, clock=lambda: NOW
        )

    @staticmethod
    def build(path: Path, monkeypatch: pytest.MonkeyPatch) -> PlanRig:
        return PlanRig(migrate(path, monkeypatch))

    def create_interval(self, *, display_name: str = "Schedule"):
        return self.triggers.create_trigger(
            OWNER,
            TriggerDraft(
                agent_instance_id=AGENT,
                display_name=display_name,
                input_text="scheduled work",
                kind=TriggerKind.INTERVAL,
                schedule=ScheduleSpec.interval(300),
                enabled=True,
                next_fire_at=NOW,
            ),
            NOW,
        )

    def create_webhook(self):
        secret = generate_secret()
        return self.triggers.create_trigger(
            OWNER,
            TriggerDraft(
                agent_instance_id=AGENT,
                display_name="Inbound hook",
                input_text="handle the delivery",
                kind=TriggerKind.WEBHOOK,
                public_id=generate_public_id(),
                secret_digest=digest_secret(secret),
                secret_created_at=NOW,
                enabled=True,
            ),
            NOW,
        )

    def create_event(self, *, event_type: str = EVENT_TYPE):
        return self.triggers.create_trigger(
            OWNER,
            TriggerDraft(
                agent_instance_id=AGENT,
                display_name="On reading",
                input_text="watch the sensor",
                kind=TriggerKind.EVENT,
                event_type=event_type,
                enabled=True,
            ),
            NOW,
        )

    def publish(
        self,
        *,
        event_id: str = "evt-1",
        payload: EventPayload = PAYLOAD,
        event_type: str = EVENT_TYPE,
    ):
        return self.service.publish(
            EventEnvelope(
                event_id=event_id,
                owner_user_id=OWNER,
                event_type=event_type,
                payload=payload,
                occurred_at=NOW,
            )
        )


def populate(rig: PlanRig, *, triggers: int = 40, occurrences: int = 40) -> tuple[Any, Any]:
    """Enough rows that a scan would be plainly visible in a plan."""
    for index in range(triggers):
        rig.create_interval(display_name=f"Schedule {index}")
    webhook = rig.create_webhook()
    trigger = rig.create_event()
    for index in range(occurrences):
        # Every publication writes one occurrence for the matching event trigger, so the history
        # query has a populated table to page through.
        assert rig.publish(event_id=f"evt-{index}").run_created_count == 1
    return webhook, trigger


# ------------------------------------------------------------------------------------------------
# 1. The owner's trigger list
# ------------------------------------------------------------------------------------------------


def test_the_owner_trigger_list_is_one_index_seek(rig: PlanRig) -> None:
    populate(rig)

    with Captured(rig.engine) as captured:
        page = rig.triggers.list_triggers(OWNER, limit=20, before_id=None)

    assert len(page) == 20
    plan = plan_for(rig.engine, captured.one("trigger_definitions.owner_user_id = ?"))
    assert "ix_trigger_definitions_owner_user_id_id" in plan, plan
    assert "SCAN trigger_definitions" not in plan, plan


def test_the_owner_trigger_list_pages_without_re_sorting(rig: PlanRig) -> None:
    """A `before_id` page is the same index walk, narrowed -- never a sort over the owner's rows."""
    populate(rig)
    newest = rig.triggers.list_triggers(OWNER, limit=20, before_id=None)

    with Captured(rig.engine) as captured:
        page = rig.triggers.list_triggers(OWNER, limit=20, before_id=newest[-1].id)

    assert len(page) == 20
    plan = plan_for(rig.engine, captured.one("trigger_definitions.owner_user_id = ?"))
    assert "ix_trigger_definitions_owner_user_id_id" in plan, plan
    assert "SCAN trigger_definitions" not in plan, plan
    assert "TEMP B-TREE" not in plan.upper(), plan


# ------------------------------------------------------------------------------------------------
# 2. One owner-scoped trigger detail read
# ------------------------------------------------------------------------------------------------


def test_the_trigger_detail_lookup_is_one_index_seek(rig: PlanRig) -> None:
    populate(rig)
    target = rig.create_interval(display_name="Find me")

    with Captured(rig.engine) as captured:
        found = rig.triggers.get_trigger(OWNER, target.id)

    assert found.id == target.id
    plan = plan_for(rig.engine, captured.one("trigger_definitions.id = ?"))
    assert "trigger_definitions" in plan, plan
    assert "SCAN trigger_definitions" not in plan, plan
    # The lookup is owner-scoped as well as id-scoped, and the Agent join is a primary-key seek.
    assert "SCAN agent_instances" not in plan, plan


# ------------------------------------------------------------------------------------------------
# 3. One trigger's occurrence history
# ------------------------------------------------------------------------------------------------


def test_the_occurrence_history_is_one_index_seek(rig: PlanRig) -> None:
    _, trigger = populate(rig)

    with Captured(rig.engine) as captured:
        history = rig.triggers.list_occurrences(OWNER, trigger.id, limit=20, before_id=None)

    assert len(history) == 20
    plan = plan_for(
        rig.engine, captured.one("trigger_occurrences.trigger_definition_id = ?", "SELECT")
    )
    # The predicate is bounded by both the trigger and its owner, and `0008` carries a composite
    # index for each. SQLite may satisfy the read with either; what matters is that it is an index
    # seek over the occurrences table rather than a scan of it.
    assert "SEARCH trigger_occurrences USING INDEX" in plan, plan
    assert (
        "ix_trigger_occurrences_trigger_definition_id_id" in plan
        or "ix_trigger_occurrences_owner_user_id_id" in plan
    ), plan
    assert "SCAN trigger_occurrences" not in plan, plan


# ------------------------------------------------------------------------------------------------
# 4. Exact event discovery
# ------------------------------------------------------------------------------------------------


def test_event_discovery_is_one_index_seek(rig: PlanRig) -> None:
    populate(rig)

    with Captured(rig.engine) as captured:
        matches = rig.triggers.list_event_matches(
            owner_user_id=OWNER, event_type=EVENT_TYPE, limit=33
        )

    assert len(matches) == 1
    plan = plan_for(rig.engine, captured.one("trigger_definitions.event_type = ?", "SELECT"))
    assert "ix_trigger_definitions_event_lookup" in plan, plan
    assert "SCAN trigger_definitions" not in plan, plan


def test_a_non_matching_event_type_scans_no_table(rig: PlanRig) -> None:
    """A type no trigger carries is answered by the same index, not by reading every row."""
    populate(rig)

    with Captured(rig.engine) as captured:
        matches = rig.triggers.list_event_matches(
            owner_user_id=OWNER, event_type=OTHER_TYPE, limit=33
        )

    assert matches == ()
    plan = plan_for(rig.engine, captured.one("trigger_definitions.event_type = ?", "SELECT"))
    assert "ix_trigger_definitions_event_lookup" in plan, plan
    assert "SCAN trigger_definitions" not in plan, plan


# ------------------------------------------------------------------------------------------------
# 5. Event identity lookup (the duplicate/conflict arbitration read)
# ------------------------------------------------------------------------------------------------


def test_the_event_identity_lookup_is_one_unique_index_seek(rig: PlanRig) -> None:
    populate(rig)

    with Captured(rig.engine) as captured:
        result = rig.publish(event_id="evt-7")

    assert result.duplicate_count == 1
    plan = plan_for(rig.engine, captured.one("trigger_occurrences.event_id = ?"))
    assert "ux_trigger_occurrences_event_identity" in plan, plan
    assert "SCAN trigger_occurrences" not in plan, plan


# ------------------------------------------------------------------------------------------------
# 6. Webhook locator lookup
# ------------------------------------------------------------------------------------------------


def test_the_webhook_locator_lookup_is_one_unique_index_seek(rig: PlanRig) -> None:
    webhook, _ = populate(rig)

    with Captured(rig.engine) as captured:
        found = rig.triggers.find_webhook_by_public_id(webhook.public_id or "")

    assert found is not None
    plan = plan_for(rig.engine, captured.one("trigger_definitions.public_id = ?"))
    assert "ux_trigger_definitions_public_id" in plan, plan
    assert "SCAN trigger_definitions" not in plan, plan


# ------------------------------------------------------------------------------------------------
# The proof itself: the tables these reads touch are the ones that grow
# ------------------------------------------------------------------------------------------------


def test_the_hot_path_tables_are_populated_enough_to_expose_a_scan(rig: PlanRig) -> None:
    """A guard on this module: if the tables were tiny, every plan above would be vacuous."""
    populate(rig)
    with rig.engine.connect() as connection:
        triggers = connection.scalar(text("SELECT COUNT(*) FROM trigger_definitions"))
        occurrences = connection.scalar(text("SELECT COUNT(*) FROM trigger_occurrences"))
    assert int(triggers) >= 40
    assert int(occurrences) >= 40
