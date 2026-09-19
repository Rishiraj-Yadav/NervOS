"""One-time, interval and cron arithmetic: coalescing, boundaries, and the horizon guard.

Every case here is a claim about what a *schedule* means, decided from a stored instant and a
named `now`. Nothing is read from a database and nothing is waited for, which is what makes the
misfire rules cheap enough to state exhaustively.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from nervos_core.domain.scheduling import (
    MAX_SCHEDULE_HORIZON,
    ScheduleCalculationError,
    ScheduleFailure,
    ScheduleSpec,
    calculate_cron,
    calculate_interval,
    calculate_one_time,
    initial_next_fire,
)
from nervos_core.domain.triggers import (
    INTERVAL_MAX_SECONDS,
    INTERVAL_MIN_SECONDS,
    InvalidTrigger,
)
from nervos_core.infrastructure.scheduling import CronSimWallClock, create_schedule_evaluator

WALL = CronSimWallClock()
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


# ------------------------------------------------------------------------------------------------
# One-time: fires once, for the instant it names, however late it is noticed
# ------------------------------------------------------------------------------------------------


def test_a_one_time_schedule_is_due_for_its_own_instant() -> None:
    decision = calculate_one_time(NOW, now=NOW)
    assert decision.nominal_at == NOW
    assert decision.next_fire_at is None
    assert decision.terminal is True


def test_a_one_time_schedule_noticed_three_days_late_still_names_its_own_instant() -> None:
    """The occurrence is for the instant the schedule named, not for the moment it was noticed."""
    run_at = NOW - timedelta(days=3)
    decision = calculate_one_time(run_at, now=NOW)
    assert decision.nominal_at == run_at
    assert decision.terminal is True


def test_a_one_time_schedule_that_is_not_due_yet_is_refused() -> None:
    with pytest.raises(InvalidTrigger):
        calculate_one_time(NOW + timedelta(seconds=1), now=NOW)


# ------------------------------------------------------------------------------------------------
# Interval: exact fixed-rate arithmetic, O(1) however long the outage was
# ------------------------------------------------------------------------------------------------


def test_an_interval_on_time_advances_by_exactly_one_period() -> None:
    decision = calculate_interval(NOW, interval_seconds=300, now=NOW)
    assert decision.nominal_at == NOW
    assert decision.next_fire_at == NOW + timedelta(seconds=300)
    assert decision.terminal is False


def test_an_interval_noticed_slightly_late_does_not_shift_its_phase() -> None:
    """A late tick must not walk the schedule forward: the nominal instant is the stored one."""
    decision = calculate_interval(NOW, interval_seconds=300, now=NOW + timedelta(seconds=100))
    assert decision.nominal_at == NOW
    assert decision.next_fire_at == NOW + timedelta(seconds=300)


def test_an_interval_noticed_exactly_one_period_late_names_the_new_instant() -> None:
    decision = calculate_interval(NOW, interval_seconds=300, now=NOW + timedelta(seconds=300))
    assert decision.nominal_at == NOW + timedelta(seconds=300)
    assert decision.next_fire_at == NOW + timedelta(seconds=600)


def test_a_missed_interval_coalesces_to_the_latest_missed_instant() -> None:
    """Three periods missed produces the newest of them, not the oldest, and not all three."""
    decision = calculate_interval(NOW, interval_seconds=300, now=NOW + timedelta(seconds=1000))
    assert decision.nominal_at == NOW + timedelta(seconds=900)
    assert decision.next_fire_at == NOW + timedelta(seconds=1200)


def test_a_three_day_interval_outage_produces_one_occurrence_not_eight_hundred() -> None:
    """The whole point of the O(1) formula: 864 missed periods still produce exactly one change."""
    decision = calculate_interval(NOW, interval_seconds=300, now=NOW + timedelta(days=3))
    steps = 3 * 24 * 60 * 60 // 300
    assert decision.nominal_at == NOW + timedelta(seconds=steps * 300)
    assert decision.next_fire_at == NOW + timedelta(seconds=(steps + 1) * 300)
    assert decision.next_fire_at is not None
    assert decision.next_fire_at > NOW + timedelta(days=3)


def test_the_interval_advance_is_always_strictly_in_the_future() -> None:
    """A schedule can never be immediately due again, whatever the outage length."""
    for offset in (0, 1, 299, 300, 301, 599, 600, 86_400, 86_401):
        moment = NOW + timedelta(seconds=offset)
        decision = calculate_interval(NOW, interval_seconds=300, now=moment)
        assert decision.next_fire_at is not None
        assert decision.next_fire_at > moment


def test_an_interval_whose_advance_escapes_the_calendar_is_a_typed_failure() -> None:
    """A horizon failure, not an `OverflowError` escaping a datetime constructor."""
    with pytest.raises(ScheduleCalculationError) as raised:
        calculate_interval(NOW, interval_seconds=INTERVAL_MAX_SECONDS, now=MAX_SCHEDULE_HORIZON)
    assert raised.value.failure is ScheduleFailure.INVALID


def test_interval_bounds_are_enforced_where_the_schedule_is_read() -> None:
    for seconds in (INTERVAL_MIN_SECONDS - 1, INTERVAL_MAX_SECONDS + 1):
        with pytest.raises(InvalidTrigger):
            calculate_interval(NOW, interval_seconds=seconds, now=NOW)


# ------------------------------------------------------------------------------------------------
# Cron: the nominal instant is the latest missed local match, and the next is strictly future
# ------------------------------------------------------------------------------------------------


def test_a_cron_on_time_names_its_own_instant() -> None:
    due = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
    decision = calculate_cron(due, expression="0 9 * * *", timezone="UTC", now=due, wall_clock=WALL)
    assert decision.nominal_at == due
    assert decision.next_fire_at == datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


def test_a_cron_noticed_a_few_minutes_late_still_names_the_stored_instant() -> None:
    due = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
    decision = calculate_cron(
        due,
        expression="0 9 * * *",
        timezone="UTC",
        now=due + timedelta(minutes=7),
        wall_clock=WALL,
    )
    assert decision.nominal_at == due
    assert decision.next_fire_at == datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


def test_a_missed_cron_coalesces_to_the_latest_missed_instant() -> None:
    """Two days offline produces one occurrence, for the newest missed 09:00, not the oldest."""
    decision = calculate_cron(
        datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
        expression="0 9 * * *",
        timezone="UTC",
        now=datetime(2026, 9, 14, 12, 0, tzinfo=UTC),
        wall_clock=WALL,
    )
    assert decision.nominal_at == datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
    assert decision.next_fire_at == datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


def test_a_cron_outage_does_not_replay_every_missed_instant() -> None:
    decision = calculate_cron(
        datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
        expression="0 9 * * *",
        timezone="UTC",
        now=datetime(2026, 9, 14, 12, 0, tzinfo=UTC),
        wall_clock=WALL,
    )
    assert decision.nominal_at == datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
    assert decision.next_fire_at == datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


def test_a_cron_schedule_the_library_cannot_parse_is_a_schedule_failure() -> None:
    """`April 31` passes the frozen dialect's digit-range check and has no occurrence at all."""
    with pytest.raises(ScheduleCalculationError) as raised:
        calculate_cron(
            NOW,
            expression="0 0 31 4 *",
            timezone="UTC",
            now=NOW,
            wall_clock=WALL,
        )
    assert raised.value.failure is ScheduleFailure.INVALID


def test_an_unresolvable_zone_is_a_timezone_failure_not_a_schedule_failure() -> None:
    with pytest.raises(ScheduleCalculationError) as raised:
        calculate_cron(
            NOW,
            expression="0 9 * * *",
            timezone="Mars/Olympus_Mons",
            now=NOW,
            wall_clock=WALL,
        )
    assert raised.value.failure is ScheduleFailure.TIMEZONE


def test_a_schedule_failure_never_carries_library_text() -> None:
    """The message reaches a durable skip field and an operator log, so it is composed."""
    with pytest.raises(ScheduleCalculationError) as raised:
        calculate_cron(
            NOW,
            expression="0 0 31 4 *",
            timezone="UTC",
            now=NOW,
            wall_clock=WALL,
        )
    message = str(raised.value)
    assert "cronsim" not in message.lower()
    assert "day-of-month" not in message.lower()


def test_a_cron_expression_over_a_dst_zone_keeps_its_local_intent() -> None:
    """An hourly schedule in a DST zone fires at each local hour, whatever UTC does."""
    due = datetime(2026, 9, 14, 17, 0, tzinfo=UTC)  # 13:00 in New York (EDT)
    decision = calculate_cron(
        due,
        expression="0 13 * * *",
        timezone="America/New_York",
        now=due,
        wall_clock=WALL,
    )
    assert decision.nominal_at == due
    # The next local 13:00 is a day later, which is 24 hours in UTC across a non-transition day.
    assert decision.next_fire_at == datetime(2026, 9, 15, 17, 0, tzinfo=UTC)


# ------------------------------------------------------------------------------------------------
# Seeding a schedule: the second half of the reusable calculation seam
# ------------------------------------------------------------------------------------------------


def test_a_new_one_time_schedule_keeps_the_instant_it_was_given() -> None:
    """A past instant is never moved: "due immediately" is expressed by storing that instant."""
    run_at = NOW - timedelta(minutes=5)
    assert initial_next_fire(ScheduleSpec.one_time(run_at), now=NOW, wall_clock=WALL) == run_at


def test_a_new_interval_schedule_starts_one_whole_period_from_now() -> None:
    """A schedule created at 12:00 with a five-minute interval fires at 12:05, not immediately."""
    resolved = initial_next_fire(ScheduleSpec.interval(300), now=NOW, wall_clock=WALL)
    assert resolved == NOW + timedelta(seconds=300)


def test_a_new_cron_schedule_starts_at_its_first_match_after_now() -> None:
    """Created at 12:00 for `09:00 daily`, it fires tomorrow at 09:00 — not retroactively today."""
    resolved = initial_next_fire(ScheduleSpec.cron("0 9 * * *", "UTC"), now=NOW, wall_clock=WALL)
    assert resolved == datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


def test_a_new_cron_schedule_is_resolved_in_its_own_zone() -> None:
    """Created at 12:00 UTC — 08:00 in New York — the next local 09:00 is the same day."""
    resolved = initial_next_fire(
        ScheduleSpec.cron("0 9 * * *", "America/New_York"), now=NOW, wall_clock=WALL
    )
    assert resolved == datetime(2026, 9, 14, 13, 0, tzinfo=UTC)
    assert resolved.astimezone(ZoneInfo("America/New_York")).replace(tzinfo=None) == (
        datetime(2026, 9, 14, 9, 0)
    )


def test_a_new_schedule_that_admits_no_occurrence_is_refused() -> None:
    with pytest.raises(ScheduleCalculationError) as raised:
        initial_next_fire(ScheduleSpec.cron("0 0 31 4 *", "UTC"), now=NOW, wall_clock=WALL)
    assert raised.value.failure is ScheduleFailure.INVALID


def test_a_new_schedule_in_an_unusable_zone_reports_a_timezone_failure() -> None:
    """A stored name the tz database has since dropped must be reported as *that*, not as a
    malformed schedule: one is a deployment fact and the other is the trigger's own fault.

    The name cannot be built through `ScheduleSpec.cron`, because the validator refuses it — which
    is the point — so the stored value is swapped in directly, exactly as a read of a long-lived
    row would encounter it.
    """
    spec = ScheduleSpec.cron("0 9 * * *", "UTC")
    object.__setattr__(spec, "timezone", _DroppedZone("Mars/Olympus_Mons"))
    with pytest.raises(ScheduleCalculationError) as raised:
        initial_next_fire(spec, now=NOW, wall_clock=WALL)
    assert raised.value.failure is ScheduleFailure.TIMEZONE


class _DroppedZone:
    """A zone name that carries its text without having been resolved."""

    def __init__(self, name: str) -> None:
        self.name = name


def test_the_evaluator_seeds_a_schedule_the_same_way() -> None:
    """The protocol member is the one E4 will call, so it must agree with the domain function."""
    evaluator = create_schedule_evaluator()
    for spec in (
        ScheduleSpec.one_time(NOW),
        ScheduleSpec.interval(600),
        ScheduleSpec.cron("0 9 * * *", "UTC"),
    ):
        assert evaluator.initial_next_fire(spec, now=NOW) == initial_next_fire(
            spec, now=NOW, wall_clock=WALL
        )
