"""The races a scheduler must lose safely: duplicates, edits, disables, deletes, and drift.

Every test here constructs the race **deterministically** — no thread, no sleep, and no timing
assumption. Two schedulers are modelled by letting the second one present a page it scanned
*before* the first one committed, which is exactly the interleaving SQLite's write lock has to
resolve; the loser is then stopped by a durable fact rather than by a lock the test had to guess at.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from nervos_core.application.agent_definitions import create_builtin_definition_registry
from nervos_core.application.scheduler import SCHEDULER_DUE_BATCH, SchedulerService
from nervos_core.application.triggers import (
    DueScheduleCandidate,
    TriggerEdit,
)
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.triggers import (
    OccurrenceStatus,
    ScheduleSpec,
    TriggerDefinition,
    TriggerKind,
)
from nervos_core.infrastructure.database.triggers import SqlAlchemyTriggerPersistence
from nervos_core.infrastructure.scheduling import create_schedule_evaluator
from scheduler_support import NOW, OWNER, Rig, cron_trigger, interval_trigger, rig
from sqlalchemy import Engine, text

__all__ = ["rig"]


class HeldPagePersistence:
    """A real persistence that answers the due scan with a page captured earlier.

    This is the whole mechanism of the race suite, and it is deliberately not a mock: the page it
    replays is one the *real* scan actually produced, and every write still goes through the real
    transaction. Holding a page while another scheduler commits is the interleaving being tested —
    it is what "scheduler B had already scanned" means concretely.
    """

    def __init__(self, inner: SqlAlchemyTriggerPersistence, page: tuple[DueScheduleCandidate, ...]):
        self._inner = inner
        self._page = page

    def due_schedule_candidates(
        self, *, now: datetime, limit: int, after: tuple[datetime, int] | None
    ) -> tuple[DueScheduleCandidate, ...]:
        return self._page

    def materialize_schedule_occurrence(self, command: Any) -> Any:
        return self._inner.materialize_schedule_occurrence(command)


def _second_scheduler(
    triggers: SqlAlchemyTriggerPersistence, page: tuple[DueScheduleCandidate, ...]
) -> SchedulerService:
    """A scheduler whose view of the due set is frozen at `page`."""
    return SchedulerService(
        create_builtin_definition_registry(),
        HeldPagePersistence(triggers, page),
        create_schedule_evaluator(),
    )


def _scan(rig: Rig, *, now: datetime = NOW) -> tuple[DueScheduleCandidate, ...]:
    return rig.triggers.due_schedule_candidates(now=now, limit=SCHEDULER_DUE_BATCH, after=None)


# ------------------------------------------------------------------------------------------------
# Two schedulers, one occurrence
# ------------------------------------------------------------------------------------------------


def test_a_second_scheduler_presenting_the_same_one_time_identity_gets_the_occurrence(
    rig: Rig,
) -> None:
    """The first scheduler completes the schedule; the second must report *done*, not *broken*.

    This is the case the ordering exists for. Materializing a one-time trigger disables it, so a
    second scheduler that applied authority before identity would raise — reporting a failure for
    work that succeeded.
    """
    trigger = rig.create(kind=TriggerKind.ONE_TIME, next_fire_at=NOW)
    page = _scan(rig)

    first = rig.tick()
    assert first.materialized == 1

    second = _second_scheduler(rig.triggers, page).tick(NOW)
    assert (second.duplicated, second.stale, second.materialized) == (1, 0, 0)
    assert rig.counts() == {"runs": 1, "jobs": 1, "attempts": 0, "occurrences": 1}
    assert rig.schedule(trigger.id).enabled is False


def test_a_second_scheduler_presenting_the_same_interval_identity_is_a_duplicate(rig: Rig) -> None:
    trigger = interval_trigger(rig)
    page = _scan(rig)

    assert rig.tick().materialized == 1
    second = _second_scheduler(rig.triggers, page).tick(NOW)
    assert (second.duplicated, second.stale) == (1, 0)
    assert rig.counts()["runs"] == 1
    assert len(rig.occurrences(trigger.id)) == 1


def test_a_second_scheduler_whose_clock_differs_loses_on_the_next_fire_expectation(
    rig: Rig,
) -> None:
    """The load-bearing case: two clocks, two nominal instants, and the unique index cannot help.

    For a recurring trigger the occurrence identity is derived from `now`, so two schedulers far
    enough apart compute *different* nominal instants for the same stored fire time. The unique
    index therefore separates nothing, and what stops the loser is that the winner's commit moved
    `next_fire_at` — the expectation the loser's transaction carries no longer holds.

    A scheduler only a little ahead computes the *same* instant, and is correctly reported as a
    duplicate instead; that is the case asserted next door, and the two together are why both the
    identity lookup and the expectation are needed.
    """
    trigger = interval_trigger(rig)
    page = _scan(rig)

    assert rig.tick().materialized == 1
    # A full interval ahead, so this scheduler names the *next* instant rather than the same one.
    later = NOW + timedelta(seconds=300)
    second = _second_scheduler(rig.triggers, page).tick(later)
    assert (second.stale, second.materialized, second.duplicated) == (1, 0, 0)
    assert rig.counts()["runs"] == 1
    assert len(rig.occurrences(trigger.id)) == 1
    # The winner's advance stands; the loser's later instant was never written.
    assert rig.schedule(trigger.id).next_fire_at == NOW + timedelta(seconds=300)


def test_a_slightly_ahead_scheduler_is_a_duplicate_rather_than_a_stale_loser(rig: Rig) -> None:
    """Within one interval the second scheduler names the same instant, so identity stops it."""
    trigger = interval_trigger(rig)
    page = _scan(rig)
    assert rig.tick().materialized == 1
    second = _second_scheduler(rig.triggers, page).tick(NOW + timedelta(seconds=120))
    assert (second.duplicated, second.stale, second.materialized) == (1, 0, 0)
    assert rig.counts()["runs"] == 1
    assert len(rig.occurrences(trigger.id)) == 1


def test_two_schedulers_over_one_due_set_produce_one_occurrence_one_run_and_one_job(
    rig: Rig,
) -> None:
    """The headline multi-scheduler property, over a mixed fleet rather than a single trigger."""
    one_time = rig.create(kind=TriggerKind.ONE_TIME, next_fire_at=NOW)
    interval = interval_trigger(rig)
    cron = cron_trigger(rig, due_at=datetime(2026, 9, 14, 9, 0, tzinfo=UTC))
    page = _scan(rig)
    assert len(page) == 3

    first = rig.tick()
    assert first.materialized == 3
    second = _second_scheduler(rig.triggers, page).tick(NOW)
    assert second.examined == 3
    assert second.materialized == 0
    assert second.effect_count == 0
    assert rig.counts()["runs"] == 3
    assert rig.counts()["jobs"] == 3
    assert rig.counts()["occurrences"] == 3
    for trigger in (one_time, interval, cron):
        assert len(rig.occurrences(trigger.id)) == 1


def test_no_scheduler_may_be_started_twice_into_a_second_run_for_one_identity(rig: Rig) -> None:
    """A third scheduler replaying the same page is still a duplicate, not a third Run."""
    trigger = rig.create(kind=TriggerKind.ONE_TIME, next_fire_at=NOW)
    page = _scan(rig)
    rig.tick()
    _second_scheduler(rig.triggers, page).tick(NOW)
    third = _second_scheduler(rig.triggers, page).tick(NOW)
    assert third.duplicated == 1
    assert rig.counts()["runs"] == 1
    assert len(rig.occurrences(trigger.id)) == 1


# ------------------------------------------------------------------------------------------------
# A duplicate of a completed identity survives the trigger being disabled
# ------------------------------------------------------------------------------------------------


def test_a_duplicate_identity_resolves_even_though_the_trigger_is_now_disabled(rig: Rig) -> None:
    """The recorded outcome is authoritative, whatever has happened to the trigger since."""
    trigger = rig.create(kind=TriggerKind.ONE_TIME, next_fire_at=NOW)
    initial = _scan(rig)
    rig.tick()
    assert rig.schedule(trigger.id).enabled is False

    # A scheduler that had scanned before the materialization still holds this identity.
    outcome = rig.triggers.materialize_schedule_occurrence(
        _command_from(rig, initial[0], nominal_at=NOW)
    )
    assert outcome.kind.value == "duplicated"
    assert outcome.occurrence is not None
    assert outcome.occurrence.id == rig.occurrences(trigger.id)[0]["id"]


# ------------------------------------------------------------------------------------------------
# Edit, disable and delete
# ------------------------------------------------------------------------------------------------


def test_an_edit_between_the_scan_and_the_transaction_writes_nothing(rig: Rig) -> None:
    trigger = interval_trigger(rig)
    page = _scan(rig)
    edited = rig.triggers.update_trigger(
        OWNER,
        trigger.id,
        TriggerEdit(
            display_name="Edited",
            input_text="changed",
            schedule=ScheduleSpec.interval(600),
            next_fire_at=NOW,
        ),
        NOW,
    )
    assert edited.config_revision == 2

    tick = _second_scheduler(rig.triggers, page).tick(NOW)
    assert (tick.stale, tick.materialized, tick.duplicated) == (1, 0, 0)
    assert rig.counts() == {"runs": 0, "jobs": 0, "attempts": 0, "occurrences": 0}
    state = rig.schedule(trigger.id)
    assert state.enabled is True
    assert state.next_fire_at == NOW


def test_the_next_tick_evaluates_an_edited_trigger_from_its_new_state(rig: Rig) -> None:
    trigger = interval_trigger(rig)
    page = _scan(rig)
    rig.triggers.update_trigger(
        OWNER,
        trigger.id,
        TriggerEdit(
            display_name="Edited",
            input_text="changed",
            schedule=ScheduleSpec.interval(600),
            next_fire_at=NOW,
        ),
        NOW,
    )
    assert _second_scheduler(rig.triggers, page).tick(NOW).stale == 1
    # A fresh scan sees the new revision and materializes normally.
    fresh = rig.tick()
    assert fresh.materialized == 1
    assert rig.counts()["runs"] == 1
    assert rig.schedule(trigger.id).next_fire_at == NOW + timedelta(seconds=600)


def test_a_disable_between_the_scan_and_the_transaction_writes_nothing(rig: Rig) -> None:
    trigger = interval_trigger(rig)
    page = _scan(rig)
    rig.triggers.set_enabled(OWNER, trigger.id, False, NOW)

    tick = _second_scheduler(rig.triggers, page).tick(NOW)
    assert (tick.stale, tick.materialized, tick.skipped) == (1, 0, 0)
    assert rig.counts() == {"runs": 0, "jobs": 0, "attempts": 0, "occurrences": 0}
    assert rig.schedule(trigger.id).enabled is False


def test_a_delete_between_the_scan_and_the_transaction_writes_nothing(rig: Rig) -> None:
    trigger = interval_trigger(rig)
    page = _scan(rig)
    rig.triggers.delete_trigger(OWNER, trigger.id)

    tick = _second_scheduler(rig.triggers, page).tick(NOW)
    assert (tick.stale, tick.materialized, tick.duplicated) == (1, 0, 0)
    assert rig.counts() == {"runs": 0, "jobs": 0, "attempts": 0, "occurrences": 0}
    assert rig.run_ids() == []


def test_a_fresh_identity_on_a_disabled_trigger_is_still_refused(rig: Rig) -> None:
    """The reorder narrows nothing for a *new* occurrence: authority still applies in full.

    The identity-first ordering exists so a duplicate is not mistaken for a new occurrence — not so
    that a disabled trigger can be materialized. A page naming an instant that was never
    materialized must still write nothing.
    """
    trigger = interval_trigger(rig)
    page = _scan(rig)
    rig.triggers.set_enabled(OWNER, trigger.id, False, NOW)

    # The page's instant, offset so it is a genuinely new identity against the disabled trigger.
    outcome = rig.triggers.materialize_schedule_occurrence(
        _command_from(rig, page[0], nominal_at=NOW - timedelta(seconds=300))
    )
    assert outcome.kind.value == "stale"
    assert outcome.stale_reason is not None
    assert outcome.stale_reason.value == "disabled"
    assert rig.counts() == {"runs": 0, "jobs": 0, "attempts": 0, "occurrences": 0}


# ------------------------------------------------------------------------------------------------
# Agent-definition drift
# ------------------------------------------------------------------------------------------------


class StaleDefinitionResolver:
    """Resolves the Agent Definition that was current when the trigger was read.

    This is what "the definition changed between resolution and the transaction" means concretely:
    the scheduler holds a resolved identity that the durable row no longer agrees with. Pinning the
    resolver is how a test produces that interleaving without a thread or a sleep.
    """

    def __init__(self, inner: Any, stale: Any) -> None:
        self._inner = inner
        self._stale = stale

    def resolve(self, definition_id: Any) -> Any:
        return self._inner.resolve(self._stale)


def _drifted_scheduler(
    rig: Rig, page: tuple[DueScheduleCandidate, ...], *, stale: Any
) -> SchedulerService:
    return SchedulerService(
        StaleDefinitionResolver(create_builtin_definition_registry(), stale),
        HeldPagePersistence(rig.triggers, page),
        create_schedule_evaluator(),
    )


def test_an_agent_definition_change_between_resolution_and_commit_consumes_nothing(
    rig: Rig,
) -> None:
    """The Agent Definition is resolved outside the transaction, so its identity is re-checked in.

    Without that check the canonical helper's refusal would be indistinguishable from a disabled
    Agent, and a drift would be recorded as an `agent_disabled` skip — consuming a scheduled
    occurrence and blaming an Agent that did nothing wrong.
    """
    trigger = interval_trigger(rig)
    page = _scan(rig)
    # The trigger was created against the Agent's then-current definition; the Agent is retargeted
    # before the materialization transaction opens, and the scheduler still holds the old identity.
    rig.set_agent_definition("1")

    tick = _drifted_scheduler(rig, page, stale=AgentDefinitionId("nervos.chat", "2")).tick(NOW)
    assert (tick.stale, tick.skipped, tick.materialized) == (1, 0, 0)
    assert rig.counts() == {"runs": 0, "jobs": 0, "attempts": 0, "occurrences": 0}
    # No occasion was consumed: the schedule is untouched and still due.
    state = rig.schedule(trigger.id)
    assert state.enabled is True
    assert state.next_fire_at == NOW - timedelta(seconds=300)


def test_the_next_tick_resolves_the_changed_definition_and_materializes_normally(
    rig: Rig,
) -> None:
    """The drift is a race, not a failure: re-resolving is the entire recovery."""
    trigger = interval_trigger(rig)
    page = _scan(rig)
    rig.set_agent_definition("1")
    tick = _drifted_scheduler(rig, page, stale=AgentDefinitionId("nervos.chat", "2")).tick(NOW)
    assert tick.stale == 1

    fresh = rig.tick()
    assert (fresh.materialized, fresh.skipped, fresh.stale) == (1, 0, 0)
    assert rig.counts()["runs"] == 1
    assert rig.occurrences(trigger.id)[0]["status"] == OccurrenceStatus.RUN_CREATED.value
    # A run created under the *new* definition carries that definition's limits.
    with rig.engine.connect() as connection:
        assert connection.scalar(text("SELECT agent_definition_version FROM runs")) == "1"


# ------------------------------------------------------------------------------------------------
# Scan fairness under admission backpressure
# ------------------------------------------------------------------------------------------------


def _make_many_due(rig: Rig, count: int) -> list[TriggerDefinition]:
    return [
        interval_trigger(rig, due_at=NOW - timedelta(seconds=count - index))
        for index in range(count)
    ]


def test_a_blocked_first_page_does_not_strand_a_runnable_later_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """More due schedules than one page, with the first page permanently blocked by capacity.

    Without the scan cursor the same 32 rows would be read on every poll and the rows beyond them
    would never be examined at all. With it, a full page advances the resume point and a short page
    wraps, so every due schedule is reached.
    """
    from scheduler_support import migrate

    engine = migrate(tmp_path / "starvation.db", monkeypatch, max_pending=1)
    try:
        blocked = _build(engine, max_pending=1)
        triggers = _make_many_due(blocked, SCHEDULER_DUE_BATCH + 8)
        assert len(triggers) == SCHEDULER_DUE_BATCH + 8

        first = blocked.tick()
        assert first.examined == SCHEDULER_DUE_BATCH
        assert first.materialized == 1
        assert first.deferred_capacity == SCHEDULER_DUE_BATCH - 1
        # The last trigger in the order has not been reached yet, and holds nothing.
        assert blocked.occurrences(triggers[-1].id) == []
        assert blocked.service.cursor is not None

        second = blocked.tick()
        assert second.examined == 8, "the page beyond the first must be reached"
        assert second.deferred_capacity == 8
        # Wrapping is what makes the cursor fair rather than merely monotonic.
        assert blocked.service.cursor is None
        # No blocked candidate consumed an occurrence.
        assert blocked.counts()["runs"] == 1
        assert blocked.counts()["occurrences"] == 1
        assert not any(
            blocked.occurrences(trigger.id) for trigger in triggers if trigger.id != triggers[0].id
        )

        # Admission is a property of the caller, not of the schedule: with room, the same due set
        # drains completely, which is what proves nothing was lost by being deferred.
        unblocked = _build(engine, max_pending=1000)
        for _ in range(SCHEDULER_DUE_BATCH + 12):
            unblocked.tick()
        assert unblocked.counts()["runs"] == SCHEDULER_DUE_BATCH + 8
        for trigger in triggers:
            assert len(unblocked.occurrences(trigger.id)) == 1
    finally:
        engine.dispose()


def test_the_scan_cursor_has_no_durable_representation(rig: Rig) -> None:
    """Fairness state is process-local; it must never become a table or a column."""
    _make_many_due(rig, 3)
    rig.tick()
    with rig.engine.connect() as connection:
        names = {
            str(row[0])
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type IN ('table','index')")
            ).all()
        }
    assert not any("cursor" in name for name in names)


def test_a_restart_resets_the_cursor_without_changing_any_outcome(rig: Rig) -> None:
    """Correctness never depends on the cursor, so losing it may only cost a re-read.

    The first scheduler filled one page and left a resume point. A restarted one starts from the
    beginning, finds the same rows already honoured, and finishes the job — no duplicate for what
    is done, no loss for what is not.
    """
    triggers = _make_many_due(rig, SCHEDULER_DUE_BATCH + 2)
    first = rig.tick()
    assert first.materialized == SCHEDULER_DUE_BATCH
    assert rig.service.cursor is not None

    restarted = SchedulerService(
        create_builtin_definition_registry(), rig.triggers, create_schedule_evaluator()
    )
    assert restarted.cursor is None
    second = restarted.tick(NOW)
    assert second.materialized == 2
    assert rig.counts()["runs"] == SCHEDULER_DUE_BATCH + 2
    for trigger in triggers:
        assert len(rig.occurrences(trigger.id)) == 1


def test_a_stale_cursor_can_delay_a_scan_but_never_loses_a_trigger(rig: Rig) -> None:
    """A cursor is scan state, not authority: wrapping is the whole recovery, and it is enough."""
    due_at = NOW + timedelta(seconds=300)
    trigger = interval_trigger(rig, due_at=due_at)

    # A cursor pointing past every row, as a stale in-memory value would be after a long outage.
    # `_cursor` is private because it is not an interface: it is process-local scan state that no
    # caller may read or write, and reaching into it here is how the test says exactly that.
    rig.service._cursor = (due_at + timedelta(days=1), 10**6)  # pyright: ignore[reportPrivateUsage]
    assert rig.service.tick(due_at).examined == 0

    # Wrapping reaches the trigger again, and it is materialized exactly once.
    rig.service._cursor = None  # pyright: ignore[reportPrivateUsage]
    assert rig.service.tick(due_at).materialized == 1
    assert len(rig.occurrences(trigger.id)) == 1
    assert rig.service.tick(due_at).examined == 0


# ------------------------------------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------------------------------------


def _build(engine: Engine, *, max_pending: int) -> Rig:
    """A scheduler composition over an engine a test already migrated."""
    from scheduler_support import Rig as SchedulerRig

    triggers = SqlAlchemyTriggerPersistence(engine, max_pending=max_pending, sleep=lambda _: None)
    service = SchedulerService(
        create_builtin_definition_registry(), triggers, create_schedule_evaluator()
    )
    return SchedulerRig(engine=engine, triggers=triggers, service=service, path=Path("."))


def _command_from(rig: Rig, candidate: DueScheduleCandidate, *, nominal_at: datetime) -> Any:
    """A command shaped the way the service builds it from this candidate, for the low-level paths.

    Used only where a test needs to name an identity the service would not compute — a duplicate of
    a completed occurrence, or a fresh identity against a trigger that has since been disabled.
    """
    from nervos_core.application.triggers import (
        ScheduleMaterializationCommand,
        resolve_agent_definition,
    )
    from nervos_core.domain.scheduling import schedule_of

    trigger = rig.triggers.get_trigger(candidate.trigger.owner_user_id, candidate.trigger.id)
    definition = resolve_agent_definition(create_builtin_definition_registry(), trigger)
    schedule = schedule_of(trigger)
    assert schedule is not None
    decision = create_schedule_evaluator().decide_due(
        schedule, current_next_fire_at=candidate.expected_next_fire_at, now=NOW
    )
    return ScheduleMaterializationCommand(
        trigger_definition_id=trigger.id,
        definition=definition,
        now=NOW,
        occurred_at=NOW,
        expected_config_revision=candidate.expected_config_revision,
        expected_next_fire_at=candidate.expected_next_fire_at,
        nominal_at=nominal_at,
        next_fire_at_after=decision.next_fire_at,
    )
