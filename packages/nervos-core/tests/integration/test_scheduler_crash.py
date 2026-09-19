"""Crash atomicity: a failure at any step before the commit leaves nothing at all.

The property under test is not "the code is careful". It is that one `BEGIN IMMEDIATE` owns the
whole transition, so there is no ordering of a crash in which an occurrence exists without its Run,
a Run exists without its occurrence, or the schedule advanced without either. A design with two
transactions would need a repair worker to converge; this one needs nothing, and the tests below
are what say so.

Every injection is a `monkeypatch` on a named step, so the failure lands at a *specific* point
rather than "somewhere in the transaction".
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from nervos_core.application.agent_definitions import create_builtin_definition_registry
from nervos_core.application.scheduler import SchedulerService
from nervos_core.infrastructure.database.triggers import SqlAlchemyTriggerPersistence
from nervos_core.infrastructure.scheduling import create_schedule_evaluator
from scheduler_support import NOW, Rig, cron_trigger, interval_trigger, rig
from sqlalchemy import text

__all__ = ["rig"]

TRIGGERS_MODULE = "nervos_core.infrastructure.database.triggers"


def _boom(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("injected failure")


def _assert_nothing_happened(rig: Rig, trigger_id: int) -> None:
    """No occurrence, no Run, no Job, and a schedule that has not moved."""
    assert rig.counts() == {"runs": 0, "jobs": 0, "attempts": 0, "occurrences": 0}
    state = rig.schedule(trigger_id)
    assert state.enabled is True
    assert state.next_fire_at == NOW - timedelta(seconds=300)


def _assert_database_is_sound(rig: Rig) -> None:
    with rig.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []


def _scheduler(rig: Rig) -> SchedulerService:
    return SchedulerService(
        create_builtin_definition_registry(), rig.triggers, create_schedule_evaluator()
    )


# ------------------------------------------------------------------------------------------------
# Each injectable boundary
# ------------------------------------------------------------------------------------------------


def test_a_failure_before_the_run_is_inserted_leaves_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    trigger = interval_trigger(rig)
    monkeypatch.setattr(f"{TRIGGERS_MODULE}.insert_run_and_job_on_connection", _boom)
    with pytest.raises(RuntimeError):
        rig.tick()
    _assert_nothing_happened(rig, trigger.id)
    _assert_database_is_sound(rig)


def test_a_failure_before_the_occurrence_is_inserted_rolls_the_run_back(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Run and its Job were already inserted on the connection when this fails.

    Run and Job share one step by design — the canonical helper inserts both — so this is the
    earliest point at which a Run can exist without its occurrence. It must not survive.
    """
    trigger = interval_trigger(rig)
    monkeypatch.setattr(SqlAlchemyTriggerPersistence, "_write_occurrence", _boom)
    with pytest.raises(RuntimeError):
        rig.tick()
    _assert_nothing_happened(rig, trigger.id)
    _assert_database_is_sound(rig)


def test_a_failure_before_the_schedule_transition_rolls_everything_back(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Run, the Job and the occurrence were all written; the advance was not.

    This is the case a two-transaction design could not recover from without a repair worker: the
    occurrence would exist while the schedule still pointed at the same instant, and the next tick
    would find it "due" again. Here nothing survives the rollback.
    """
    trigger = interval_trigger(rig)
    monkeypatch.setattr(SqlAlchemyTriggerPersistence, "_apply_schedule_transition", _boom)
    with pytest.raises(RuntimeError):
        rig.tick()
    _assert_nothing_happened(rig, trigger.id)
    _assert_database_is_sound(rig)


def test_a_failure_before_the_commit_leaves_no_partial_occurrence(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last injectable step: the transaction body completed and the commit never happened."""
    trigger = interval_trigger(rig)
    monkeypatch.setattr(SqlAlchemyTriggerPersistence, "_apply_schedule_transition", _boom)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            rig.tick()
    _assert_nothing_happened(rig, trigger.id)
    _assert_database_is_sound(rig)


def test_a_terminal_skip_failure_before_the_transition_leaves_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fatal-calculation path is one transaction too, not "write the skip, then disable".

    The schedule cannot be evaluated, so the occurrence is owed and the trigger must be retired —
    and both must happen together. If the disable failed on its own, the next tick would evaluate
    the same unevaluable schedule again and write a second skip for it, for as long as the process
    ran.
    """
    trigger = cron_trigger(rig, expression="0 0 31 4 *", due_at=NOW)
    monkeypatch.setattr(SqlAlchemyTriggerPersistence, "_apply_schedule_transition", _boom)
    with pytest.raises(RuntimeError):
        rig.tick()
    assert rig.occurrences(trigger.id) == []
    assert rig.counts()["runs"] == 0
    state = rig.schedule(trigger.id)
    assert state.enabled is True
    assert state.next_fire_at is not None
    _assert_database_is_sound(rig)

    # With the fault removed the skip and the retirement commit together, exactly once.
    monkeypatch.undo()
    tick = _scheduler(rig).tick(NOW)
    assert tick.skipped == 1
    records = rig.occurrences(trigger.id)
    assert len(records) == 1
    assert records[0]["skip_code"] == "schedule_invalid"
    assert rig.schedule(trigger.id).enabled is False


# ------------------------------------------------------------------------------------------------
# Recovery, and the commit that does land
# ------------------------------------------------------------------------------------------------


def test_the_next_tick_after_a_crash_succeeds_normally(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing has to be repaired, because nothing partial was left behind."""
    trigger = interval_trigger(rig)
    # The private method is the injection seam: naming it is how the test picks one step.
    original = SqlAlchemyTriggerPersistence._apply_schedule_transition  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(SqlAlchemyTriggerPersistence, "_apply_schedule_transition", _boom)
    with pytest.raises(RuntimeError):
        rig.tick()

    monkeypatch.setattr(SqlAlchemyTriggerPersistence, "_apply_schedule_transition", original)
    tick = _scheduler(rig).tick(NOW)
    assert (tick.materialized, tick.stale, tick.duplicated) == (1, 0, 0)
    assert rig.counts() == {"runs": 1, "jobs": 1, "attempts": 0, "occurrences": 1}
    assert rig.occurrences(trigger.id)[0]["run_id"] is not None
    assert rig.schedule(trigger.id).next_fire_at == NOW + timedelta(seconds=300)
    _assert_database_is_sound(rig)


def test_after_the_commit_every_durable_piece_exists_together(rig: Rig) -> None:
    trigger = interval_trigger(rig)
    assert rig.tick().materialized == 1
    assert rig.counts() == {"runs": 1, "jobs": 1, "attempts": 0, "occurrences": 1}
    occurrence = rig.occurrences(trigger.id)[0]
    assert occurrence["run_id"] == rig.run_ids()[0]
    assert rig.schedule(trigger.id).next_fire_at == NOW + timedelta(seconds=300)
    _assert_database_is_sound(rig)


def test_no_pending_occurrence_state_can_exist(rig: Rig) -> None:
    """The status vocabulary has no third member, so a partial occurrence is unrepresentable."""
    rig.tick()
    with rig.engine.connect() as connection:
        statuses = {
            str(value)
            for value in connection.scalars(text("SELECT DISTINCT status FROM trigger_occurrences"))
        }
    assert statuses <= {"run_created", "skipped"}


def test_no_repair_worker_or_reconciliation_surface_is_needed(rig: Rig) -> None:
    """A crashed materialization leaves no trace for anything to sweep."""
    _ = rig
    session = SqlAlchemyTriggerPersistence  # the seam a repair worker would have to live beside
    assert not hasattr(session, "reconcile_pending_occurrences")
    assert not hasattr(session, "close_pending_occurrences")
