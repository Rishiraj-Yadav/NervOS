"""The frozen DST contract, asserted against NervOS behaviour rather than library behaviour.

ADR 0018 fixes three cases, and the middle one is the reason this file exists rather than a
one-line assertion that "the library handles DST":

* a local time that does not exist fires **once, at the end of the gap**;
* a local time that happens twice fires **once, on the first occurrence**;
* a schedule coarser than the shift keeps its **local intent**, so the shift moves the UTC instant
  and never the wall-clock reading.

Every case asserts the instant NervOS produces. Where the library is also involved, its own answer
is asserted separately, so an upstream change fails loudly here instead of quietly altering
scheduling behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from nervos_core.domain.scheduling import calculate_cron, resolve_local
from nervos_core.domain.triggers import InvalidTrigger
from nervos_core.infrastructure.scheduling import CronSimWallClock

WALL = CronSimWallClock()
NEW_YORK = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")
LORD_HOWE = ZoneInfo("Australia/Lord_Howe")
UTC_ZONE = ZoneInfo("UTC")


# ------------------------------------------------------------------------------------------------
# Spring forward: the reading does not exist, and the occurrence is owed at the gap's end
# ------------------------------------------------------------------------------------------------


def test_a_nonexistent_new_york_reading_resolves_to_the_end_of_the_gap() -> None:
    """02:30 on 2026-03-08 never happens, so the occurrence is owed the moment 03:00 arrives."""
    resolved = resolve_local(datetime(2026, 3, 8, 2, 30), NEW_YORK)
    assert resolved == datetime(2026, 3, 8, 7, 0, tzinfo=UTC)
    assert resolved.astimezone(NEW_YORK).replace(tzinfo=None) == datetime(2026, 3, 8, 3, 0)


def test_the_gap_end_is_not_a_zoneinfo_fold_reading() -> None:
    """The reason the transition is searched for: neither fold value is the contract's instant.

    `fold=0` would answer 07:30 and `fold=1` would answer 06:30, and the frozen rule is 07:00. A
    future refactor that "simplifies" the search into a bare `fold=0` fails here.
    """
    target = datetime(2026, 3, 8, 2, 30)
    folded_zero = target.replace(tzinfo=NEW_YORK).astimezone(UTC)
    folded_one = target.replace(tzinfo=NEW_YORK, fold=1).astimezone(UTC)
    assert folded_zero == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)
    assert folded_one == datetime(2026, 3, 8, 6, 30, tzinfo=UTC)
    assert resolve_local(target, NEW_YORK) not in (folded_zero, folded_one)


def test_a_gap_cron_fires_once_at_the_gap_end_and_advances_local() -> None:
    decision = calculate_cron(
        datetime(2026, 3, 8, 7, 0, tzinfo=UTC),
        expression="30 2 * * *",
        timezone="America/New_York",
        now=datetime(2026, 3, 8, 7, 5, tzinfo=UTC),
        wall_clock=WALL,
    )
    assert decision.nominal_at == datetime(2026, 3, 8, 7, 0, tzinfo=UTC)
    # The next 02:30 is the following day, which is EDT: 06:30 UTC.
    assert decision.next_fire_at == datetime(2026, 3, 9, 6, 30, tzinfo=UTC)


def test_a_spring_gap_in_london_resolves_to_its_own_transition() -> None:
    """2026-03-29 skips 01:00 to 02:00 local, so 01:30 is owed at 01:00 UTC."""
    assert resolve_local(datetime(2026, 3, 29, 1, 30), LONDON) == datetime(
        2026, 3, 29, 1, 0, tzinfo=UTC
    )


def test_a_thirty_minute_shift_is_not_assumed_to_be_an_hour() -> None:
    """Lord Howe moves by thirty minutes, so a one-hour assumption would miss by half."""
    resolved = resolve_local(datetime(2026, 10, 4, 2, 15), LORD_HOWE)
    assert resolved == datetime(2026, 10, 3, 15, 30, tzinfo=UTC)
    assert resolved.astimezone(LORD_HOWE).replace(tzinfo=None) == datetime(2026, 10, 4, 2, 30)


# ------------------------------------------------------------------------------------------------
# Fall back: the reading happens twice, and the occurrence is owed once, on the first
# ------------------------------------------------------------------------------------------------


def test_a_repeated_new_york_reading_resolves_to_the_first_occurrence() -> None:
    """01:30 happens twice on 2026-11-01; the occurrence is for the earlier (still EDT) one."""
    resolved = resolve_local(datetime(2026, 11, 1, 1, 30), NEW_YORK)
    assert resolved == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert resolved.astimezone(NEW_YORK).replace(tzinfo=None) == datetime(2026, 11, 1, 1, 30)


def test_a_repeated_reading_produces_one_occurrence_not_two() -> None:
    """Iteration happens on the wall clock, where the reading occurs once — nothing to suppress.

    The second pass would be 06:30 UTC. Asserting the next fire time skips it is what proves the
    suppression is structural rather than a comparison that happened to work.
    """
    decision = calculate_cron(
        datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
        expression="30 1 * * *",
        timezone="America/New_York",
        now=datetime(2026, 11, 1, 5, 35, tzinfo=UTC),
        wall_clock=WALL,
    )
    assert decision.nominal_at == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert decision.next_fire_at == datetime(2026, 11, 2, 6, 30, tzinfo=UTC)


def test_a_fall_back_in_london_resolves_to_its_own_first_occurrence() -> None:
    """2026-10-25 repeats 01:00 to 02:00 local, so 01:30 is owed at 00:30 UTC (BST)."""
    assert resolve_local(datetime(2026, 10, 25, 1, 30), LONDON) == datetime(
        2026, 10, 25, 0, 30, tzinfo=UTC
    )


def test_a_repeated_thirty_minute_reading_also_resolves_once() -> None:
    """Lord Howe repeats half an hour rather than a whole one.

    The first occurrence on 2026-04-05 is still on summer time (+11:00), so resolving to it is
    observable as the DST offset still applying to the reading.
    """
    resolved = resolve_local(datetime(2026, 4, 5, 1, 45), LORD_HOWE)
    assert resolved == datetime(2026, 4, 4, 14, 45, tzinfo=UTC)
    reading = resolved.astimezone(LORD_HOWE)
    assert reading.replace(tzinfo=None) == datetime(2026, 4, 5, 1, 45)
    assert reading.utcoffset() == timedelta(hours=11)


# ------------------------------------------------------------------------------------------------
# Coarser than the shift: the local intent is preserved
# ------------------------------------------------------------------------------------------------


def test_a_daily_noon_schedule_moves_its_utc_instant_by_the_shift_not_its_reading() -> None:
    """The nominal instant is computed in the zone, so the shift moves UTC and nothing else."""
    before = calculate_cron(
        datetime(2026, 10, 31, 16, 0, tzinfo=UTC),  # 12:00 EDT
        expression="0 12 * * *",
        timezone="America/New_York",
        now=datetime(2026, 10, 31, 16, 5, tzinfo=UTC),
        wall_clock=WALL,
    )
    after = calculate_cron(
        datetime(2026, 11, 1, 17, 0, tzinfo=UTC),  # 12:00 EST
        expression="0 12 * * *",
        timezone="America/New_York",
        now=datetime(2026, 11, 1, 17, 5, tzinfo=UTC),
        wall_clock=WALL,
    )
    assert before.nominal_at == datetime(2026, 10, 31, 16, 0, tzinfo=UTC)
    assert after.nominal_at == datetime(2026, 11, 1, 17, 0, tzinfo=UTC)
    for decision in (before, after):
        assert decision.nominal_at.astimezone(NEW_YORK).replace(tzinfo=None).hour == 12


# ------------------------------------------------------------------------------------------------
# Normal days, and the resolution contract itself
# ------------------------------------------------------------------------------------------------


def test_an_ordinary_reading_resolves_to_the_obvious_instant() -> None:
    assert resolve_local(datetime(2026, 9, 14, 9, 0), NEW_YORK) == datetime(
        2026, 9, 14, 13, 0, tzinfo=UTC
    )
    assert resolve_local(datetime(2026, 9, 14, 9, 0), UTC_ZONE) == datetime(
        2026, 9, 14, 9, 0, tzinfo=UTC
    )


def test_the_days_around_a_transition_resolve_without_special_cases() -> None:
    """The minute before and the minute after each shift is ordinary, and must stay ordinary."""
    for reading in (
        datetime(2026, 3, 8, 1, 59),
        datetime(2026, 3, 8, 3, 0),
        datetime(2026, 11, 1, 0, 59),
        datetime(2026, 11, 1, 2, 0),
    ):
        resolved = resolve_local(reading, NEW_YORK)
        assert resolved.astimezone(NEW_YORK).replace(tzinfo=None) == reading


def test_resolution_refuses_an_already_aware_reading() -> None:
    """A wall-clock reading is naive by definition; an aware one is the other thing."""
    with pytest.raises(InvalidTrigger):
        resolve_local(datetime(2026, 9, 14, 9, 0, tzinfo=UTC), NEW_YORK)


def test_resolution_never_depends_on_the_machine_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The contract follows the zone the trigger names, not the host's `TZ`."""
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    assert resolve_local(datetime(2026, 3, 8, 2, 30), NEW_YORK) == datetime(
        2026, 3, 8, 7, 0, tzinfo=UTC
    )
