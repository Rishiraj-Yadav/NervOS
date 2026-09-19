"""The scheduler service against a real database: what each tick decides and what it commits.

The unit suites prove the arithmetic. This suite proves the arithmetic reaches durable state with
the right shape — one occurrence, one Run, one Job, and a schedule that moved exactly once — and
that the four ways a candidate can be refused leave no trace at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from nervos_core.application.agent_definitions import create_builtin_definition_registry
from nervos_core.application.errors import QueueCapacityExceeded
from nervos_core.application.scheduler import SCHEDULER_DUE_BATCH, SchedulerService, SchedulerTick
from nervos_core.application.triggers import (
    ResolvedAgentDefinition,
    ScheduleMaterializationCommand,
    ScheduleMaterializationOutcome,
    ScheduleOutcomeKind,
    StaleReason,
    TriggerDraft,
    TriggerMaterializationCommand,
    TriggerNotEditable,
)
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.runs import TOOL_ENABLED_LIMITS
from nervos_core.domain.triggers import (
    InvalidTrigger,
    OccurrenceStatus,
    ScheduleSpec,
    SkipReason,
    TriggerKind,
)
from nervos_core.infrastructure.database.models import TriggerDefinitionRecord
from nervos_core.infrastructure.database.triggers import SqlAlchemyTriggerPersistence
from nervos_core.infrastructure.scheduling import create_schedule_evaluator
from nervos_core.infrastructure.security.webhook_secrets import (
    digest_secret,
    generate_public_id,
    generate_secret,
)
from scheduler_support import (
    AGENT,
    NOW,
    OWNER,
    Rig,
    cron_trigger,
    interval_trigger,
    migrate,
    rig,
)
from sqlalchemy import Engine, select, text

__all__ = ["rig"]


# ------------------------------------------------------------------------------------------------
# SchedulerTick is an accounting, and the accounting has to add up
# ------------------------------------------------------------------------------------------------


def test_a_tick_whose_counters_do_not_account_for_every_candidate_is_refused() -> None:
    with pytest.raises(ValueError):
        SchedulerTick(
            examined=3,
            materialized=1,
            skipped=0,
            duplicated=0,
            stale=0,
            deferred_capacity=0,
            deferred_contention=0,
        )


def test_a_tick_reports_effects_separately_from_races() -> None:
    tick = SchedulerTick(
        examined=4,
        materialized=1,
        skipped=1,
        duplicated=1,
        stale=1,
        deferred_capacity=0,
        deferred_contention=0,
    )
    assert tick.effect_count == 2
    assert tick.deferred is False


def test_a_positive_scheduler_batch_is_required() -> None:
    from nervos_core.application.agent_definitions import create_builtin_definition_registry

    with pytest.raises(ValueError):
        SchedulerService(create_builtin_definition_registry(), object(), object(), batch=0)  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------------
# One-time
# ------------------------------------------------------------------------------------------------


def test_a_due_one_time_trigger_materializes_once_and_completes(rig: Rig) -> None:
    trigger = rig.create(kind=TriggerKind.ONE_TIME, next_fire_at=NOW)
    tick = rig.tick()
    assert (tick.materialized, tick.skipped, tick.duplicated, tick.stale) == (1, 0, 0, 0)
    records = rig.occurrences(trigger.id)
    assert len(records) == 1
    assert records[0]["status"] == OccurrenceStatus.RUN_CREATED.value
    assert records[0]["nominal_at"] is not None
    assert rig.counts() == {"runs": 1, "jobs": 1, "attempts": 0, "occurrences": 1}
    state = rig.schedule(trigger.id)
    assert state.enabled is False
    assert state.next_fire_at is None


def test_a_one_time_trigger_noticed_three_days_late_fires_once(rig: Rig) -> None:
    trigger = rig.create(kind=TriggerKind.ONE_TIME, next_fire_at=NOW - timedelta(days=3))
    tick = rig.tick()
    assert tick.materialized == 1
    assert len(rig.occurrences(trigger.id)) == 1
    assert rig.counts()["runs"] == 1


def test_a_completed_one_time_trigger_is_not_scanned_again(rig: Rig) -> None:
    rig.create(kind=TriggerKind.ONE_TIME, next_fire_at=NOW)
    assert rig.tick().materialized == 1
    second = rig.tick(NOW + timedelta(days=1))
    assert second.examined == 0
    assert rig.counts()["runs"] == 1


# ------------------------------------------------------------------------------------------------
# Interval
# ------------------------------------------------------------------------------------------------


def test_a_due_interval_trigger_materializes_and_advances(rig: Rig) -> None:
    trigger = interval_trigger(rig)
    tick = rig.tick()
    assert tick.materialized == 1
    state = rig.schedule(trigger.id)
    assert state.enabled is True
    assert state.next_fire_at == NOW + timedelta(seconds=300)
    assert state.config_revision == 1


def test_a_missed_interval_coalesces_to_one_catch_up_run(rig: Rig) -> None:
    trigger = interval_trigger(rig, seconds=300, due_at=NOW - timedelta(days=3))
    tick = rig.tick()
    assert tick.materialized == 1
    assert rig.counts()["runs"] == 1
    records = rig.occurrences(trigger.id)
    assert len(records) == 1
    state = rig.schedule(trigger.id)
    assert state.next_fire_at is not None and state.next_fire_at > NOW


def test_an_interval_trigger_that_is_not_due_is_never_examined(rig: Rig) -> None:
    interval_trigger(rig, due_at=NOW + timedelta(seconds=1))
    assert rig.tick().examined == 0


def test_a_disabled_schedule_is_never_examined(rig: Rig) -> None:
    rig.create(kind=TriggerKind.INTERVAL, enabled=False)
    assert rig.tick().examined == 0


def test_a_delivery_driven_trigger_is_never_examined(rig: Rig) -> None:
    rig.triggers.create_trigger(
        OWNER,
        TriggerDraft(
            agent_instance_id=AGENT,
            display_name="On delivery",
            input_text="handle",
            kind=TriggerKind.WEBHOOK,
            public_id=generate_public_id(),
            secret_digest=digest_secret(generate_secret()),
            secret_created_at=NOW,
        ),
        NOW,
    )
    assert rig.tick().examined == 0


# ------------------------------------------------------------------------------------------------
# Cron
# ------------------------------------------------------------------------------------------------


def test_a_due_cron_trigger_materializes_and_advances_to_the_next_local_match(rig: Rig) -> None:
    trigger = cron_trigger(rig, due_at=datetime(2026, 9, 14, 9, 0, tzinfo=UTC))
    tick = rig.tick()
    assert tick.materialized == 1
    state = rig.schedule(trigger.id)
    assert state.next_fire_at == datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


def test_a_missed_cron_coalesces_to_one_catch_up_run(rig: Rig) -> None:
    trigger = cron_trigger(rig, due_at=datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
    tick = rig.tick()
    assert tick.materialized == 1
    assert rig.counts()["runs"] == 1
    records = rig.occurrences(trigger.id)
    assert len(records) == 1
    assert records[0]["nominal_at"] is not None
    state = rig.schedule(trigger.id)
    assert state.next_fire_at == datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


def test_a_cron_that_cannot_be_evaluated_is_recorded_as_a_skip_and_disables(
    rig: Rig,
) -> None:
    """`April 31` passes the frozen dialect and has no occurrence, so the trigger is retired."""
    trigger = cron_trigger(rig, expression="0 0 31 4 *", due_at=NOW)
    tick = rig.tick()
    assert (tick.skipped, tick.materialized, tick.stale) == (1, 0, 0)
    records = rig.occurrences(trigger.id)
    assert len(records) == 1
    assert records[0]["status"] == OccurrenceStatus.SKIPPED.value
    assert records[0]["skip_code"] == SkipReason.SCHEDULE_INVALID.value
    assert records[0]["run_id"] is None
    assert rig.counts()["runs"] == 0
    assert rig.schedule(trigger.id).enabled is False


def test_an_unresolvable_timezone_is_recorded_with_its_own_skip_reason(rig: Rig) -> None:
    """A zone name that resolved when it was entered and does not resolve now.

    The failure cannot be created through the API — the domain validator would refuse the name at
    entry, which is the point — so it is injected onto the row. That is exactly the deployment
    shape the contract is about: the trigger was written months ago and the host's tz database has
    since stopped supplying the zone.
    """
    trigger = cron_trigger(rig, due_at=NOW)
    _store_timezone(rig, trigger.id, "Mars/Olympus_Mons")
    tick = rig.tick()
    assert (tick.skipped, tick.materialized, tick.stale) == (1, 0, 0)
    records = rig.occurrences(trigger.id)
    assert len(records) == 1
    assert records[0]["status"] == OccurrenceStatus.SKIPPED.value
    assert records[0]["skip_code"] == SkipReason.SCHEDULE_TIMEZONE_INVALID.value
    assert rig.counts()["runs"] == 0
    state = rig.schedule(trigger.id)
    assert state.enabled is False
    assert state.next_fire_at is None


def test_a_retired_schedule_is_not_examined_on_the_next_tick(rig: Rig) -> None:
    cron_trigger(rig, expression="0 0 31 4 *", due_at=NOW)
    assert rig.tick().skipped == 1
    assert rig.tick(NOW + timedelta(hours=1)).examined == 0


def test_a_skip_message_never_carries_library_text(rig: Rig) -> None:
    trigger = cron_trigger(rig, expression="0 0 31 4 *", due_at=NOW)
    rig.tick()
    message = str(rig.occurrences(trigger.id)[0]["skip_code"])
    assert "cronsim" not in message.lower()


# ------------------------------------------------------------------------------------------------
# Agent eligibility
# ------------------------------------------------------------------------------------------------


def test_a_disabled_agent_completes_a_one_time_schedule_without_a_run(rig: Rig) -> None:
    trigger = rig.create(kind=TriggerKind.ONE_TIME, next_fire_at=NOW)
    rig.set_agent_enabled(False)
    tick = rig.tick()
    assert (tick.skipped, tick.materialized) == (1, 0)
    records = rig.occurrences(trigger.id)
    assert records[0]["status"] == OccurrenceStatus.SKIPPED.value
    assert records[0]["skip_code"] == SkipReason.AGENT_DISABLED.value
    assert rig.counts()["runs"] == 0
    state = rig.schedule(trigger.id)
    assert state.enabled is False
    assert state.next_fire_at is None


def test_a_disabled_agent_does_not_end_a_recurring_schedule(rig: Rig) -> None:
    """The trigger stays enabled and keeps its phase: the Agent is disabled, not the automation."""
    trigger = interval_trigger(rig)
    rig.set_agent_enabled(False)
    tick = rig.tick()
    assert tick.skipped == 1
    assert rig.occurrences(trigger.id)[0]["skip_code"] == SkipReason.AGENT_DISABLED.value
    state = rig.schedule(trigger.id)
    assert state.enabled is True
    assert state.next_fire_at == NOW + timedelta(seconds=300)


def test_a_schedule_resumes_normally_once_the_agent_is_eligible_again(rig: Rig) -> None:
    trigger = interval_trigger(rig)
    rig.set_agent_enabled(False)
    rig.tick()
    rig.set_agent_enabled(True)
    rig.tick(NOW + timedelta(seconds=300))
    statuses = [row["status"] for row in rig.occurrences(trigger.id)]
    assert statuses == [
        OccurrenceStatus.SKIPPED.value,
        OccurrenceStatus.RUN_CREATED.value,
    ]


# ------------------------------------------------------------------------------------------------
# Admission backpressure
# ------------------------------------------------------------------------------------------------


def test_admission_backpressure_rolls_back_and_leaves_the_schedule_due(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is written and nothing is consumed: the occurrence is owed again on the next tick."""
    engine = migrate(tmp_path / "capacity.db", monkeypatch, max_pending=1)
    try:
        triggers = SqlAlchemyTriggerPersistence(engine, max_pending=1, sleep=lambda _: None)
        service = _service(triggers)
        first = triggers.create_trigger(OWNER, _interval_draft(), NOW)
        second = triggers.create_trigger(OWNER, _interval_draft(display_name="Second"), NOW)
        tick = service.tick(NOW)
        assert tick.examined == 2
        assert (tick.materialized, tick.deferred_capacity, tick.skipped) == (1, 1, 0)
        assert tick.deferred is True
        # Exactly one Run and one occurrence exist: the deferred candidate wrote nothing at all.
        assert _count(engine, "runs") == 1
        assert _count(engine, "trigger_occurrences") == 1
        assert _count_for(engine, "trigger_occurrences", second.id) == 0
        # The deferred trigger still holds its original, already-due instant; the other advanced.
        assert _next_fire(engine, second.id) == NOW
        assert _next_fire(engine, first.id) == NOW + timedelta(seconds=300)
    finally:
        engine.dispose()


def test_a_capacity_refusal_is_raised_before_any_occurrence_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proves the rollback is whole: the refusal leaves the trigger exactly as it was."""
    engine = migrate(tmp_path / "capacity-rollback.db", monkeypatch, max_pending=1)
    try:
        triggers = SqlAlchemyTriggerPersistence(engine, max_pending=1, sleep=lambda _: None)
        trigger = triggers.create_trigger(OWNER, _interval_draft(), NOW)
        # One Run already occupies the only slot, so the next submission cannot be admitted.
        triggers.materialize_occurrence_and_run(
            _command(trigger.id, nominal_at=NOW - timedelta(seconds=300))
        )
        assert _count(engine, "trigger_occurrences") == 1

        with pytest.raises(QueueCapacityExceeded):
            triggers.materialize_occurrence_and_run(_command(trigger.id, nominal_at=NOW))
        # The refused attempt wrote no second occurrence and moved nothing.
        assert _count(engine, "trigger_occurrences") == 1
        assert _count(engine, "runs") == 1
        assert _next_fire(engine, trigger.id) == NOW
    finally:
        engine.dispose()


# ------------------------------------------------------------------------------------------------
# Bounds and ordering
# ------------------------------------------------------------------------------------------------


def test_a_tick_examines_at_most_one_bounded_page(rig: Rig) -> None:
    for index in range(SCHEDULER_DUE_BATCH + 5):
        interval_trigger(rig, due_at=NOW - timedelta(seconds=index + 1))
    tick = rig.tick()
    assert tick.examined == SCHEDULER_DUE_BATCH
    assert tick.examined + tick.effect_count >= SCHEDULER_DUE_BATCH


def test_the_due_page_is_ordered_oldest_first(rig: Rig) -> None:
    newest = interval_trigger(rig, due_at=NOW - timedelta(seconds=1))
    oldest = interval_trigger(rig, due_at=NOW - timedelta(seconds=300))
    middle = interval_trigger(rig, due_at=NOW - timedelta(seconds=60))
    candidates = rig.triggers.due_schedule_candidates(now=NOW, limit=3, after=None)
    assert [candidate.trigger.id for candidate in candidates] == [oldest.id, middle.id, newest.id]


def test_the_due_scan_never_returns_a_trigger_that_is_not_due(rig: Rig) -> None:
    interval_trigger(rig, due_at=NOW + timedelta(seconds=1))
    assert rig.triggers.due_schedule_candidates(now=NOW, limit=10, after=None) == ()


# ------------------------------------------------------------------------------------------------
# The command's own invariants
# ------------------------------------------------------------------------------------------------


def test_a_schedule_command_refuses_a_future_nominal_instant(rig: Rig) -> None:
    from nervos_core.application.triggers import ResolvedAgentDefinition
    from nervos_core.domain.agents import AgentDefinitionId
    from nervos_core.domain.runs import TOOL_ENABLED_LIMITS

    with pytest.raises(InvalidTrigger):
        ScheduleMaterializationCommand(
            trigger_definition_id=1,
            definition=ResolvedAgentDefinition(
                definition_id=AgentDefinitionId("nervos.chat", "2"), limits=TOOL_ENABLED_LIMITS
            ),
            now=NOW,
            occurred_at=NOW,
            expected_config_revision=1,
            expected_next_fire_at=NOW,
            nominal_at=NOW + timedelta(seconds=1),
        )


def test_a_schedule_command_refuses_a_next_fire_time_that_is_not_in_the_future(rig: Rig) -> None:
    from nervos_core.application.triggers import ResolvedAgentDefinition
    from nervos_core.domain.agents import AgentDefinitionId
    from nervos_core.domain.runs import TOOL_ENABLED_LIMITS

    with pytest.raises(InvalidTrigger):
        ScheduleMaterializationCommand(
            trigger_definition_id=1,
            definition=ResolvedAgentDefinition(
                definition_id=AgentDefinitionId("nervos.chat", "2"), limits=TOOL_ENABLED_LIMITS
            ),
            now=NOW,
            occurred_at=NOW,
            expected_config_revision=1,
            expected_next_fire_at=NOW,
            nominal_at=NOW,
            next_fire_at_after=NOW,
        )


def test_a_stale_outcome_must_carry_a_reason_and_no_occurrence() -> None:

    with pytest.raises(InvalidTrigger):
        ScheduleMaterializationOutcome(kind=ScheduleOutcomeKind.STALE, stale_reason=None)
    with pytest.raises(InvalidTrigger):
        ScheduleMaterializationOutcome(kind=ScheduleOutcomeKind.STALE)


def test_the_stale_vocabulary_is_complete() -> None:
    assert {reason.value for reason in StaleReason} == {
        "revision_changed",
        "next_fire_changed",
        "disabled",
        "deleted",
        "not_schedule",
        "not_due",
        "agent_definition_changed",
    }


# ------------------------------------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------------------------------------


def _interval_draft(*, display_name: str = "Interval") -> TriggerDraft:
    return TriggerDraft(
        agent_instance_id=AGENT,
        display_name=display_name,
        input_text="scheduled work",
        kind=TriggerKind.INTERVAL,
        schedule=ScheduleSpec.interval(300),
        next_fire_at=NOW,
    )


def _command(trigger_id: int, *, nominal_at: datetime) -> TriggerMaterializationCommand:
    return TriggerMaterializationCommand(
        trigger_definition_id=trigger_id,
        definition=ResolvedAgentDefinition(
            definition_id=AgentDefinitionId("nervos.chat", "2"), limits=TOOL_ENABLED_LIMITS
        ),
        now=NOW,
        occurred_at=NOW,
        nominal_at=nominal_at,
    )


def _service(triggers: SqlAlchemyTriggerPersistence) -> SchedulerService:
    return SchedulerService(
        create_builtin_definition_registry(), triggers, create_schedule_evaluator()
    )


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(connection.scalar(text(f"SELECT count(*) FROM {table}")) or 0)


def _count_for(engine: Engine, table: str, trigger_id: int) -> int:
    with engine.connect() as connection:
        return int(
            connection.scalar(
                text(f"SELECT count(*) FROM {table} WHERE trigger_definition_id = :id"),
                {"id": trigger_id},
            )
            or 0
        )


def _store_timezone(rig: Rig, trigger_id: int, timezone: str) -> None:
    """Write a zone name straight onto the row, bypassing the validator that would refuse it."""
    with rig.engine.begin() as connection:
        connection.execute(
            text("UPDATE trigger_definitions SET timezone = :tz WHERE id = :id"),
            {"tz": timezone, "id": trigger_id},
        )


def _next_fire(engine: Engine, trigger_id: int) -> datetime | None:
    with engine.connect() as connection:
        value: datetime | None = connection.scalar(
            select(TriggerDefinitionRecord.next_fire_at).where(
                TriggerDefinitionRecord.id == trigger_id
            )
        )
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def test_a_completed_one_time_trigger_cannot_be_re_enabled(rig: Rig) -> None:
    """The durable guard that makes "one occurrence per one-time schedule" hold without an API.

    The trigger management surface arrives in a later milestone, so the invariant cannot live
    there. It lives in the persistence service, where every caller must pass it: once a one-time
    trigger has an occurrence, re-enabling it would need a second next fire time, and a second next
    fire time would be a second occurrence for a schedule that only ever had one.
    """
    trigger = rig.create(kind=TriggerKind.ONE_TIME, next_fire_at=NOW)
    assert rig.tick().materialized == 1
    with pytest.raises(TriggerNotEditable):
        rig.triggers.set_enabled(OWNER, trigger.id, True, NOW, next_fire_at=NOW)

    # And there is nothing left for the scheduler to find, however often it looks.
    assert rig.schedule(trigger.id).enabled is False
    assert rig.tick(NOW + timedelta(days=1)).examined == 0
    assert rig.counts() == {"runs": 1, "jobs": 1, "attempts": 0, "occurrences": 1}
