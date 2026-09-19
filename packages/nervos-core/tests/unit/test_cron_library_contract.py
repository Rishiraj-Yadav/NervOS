"""The `cronsim` adapter's contract, asserted independently of NervOS schedule policy.

Two things are being pinned here, and they are different kinds of thing.

**That the adapter answers the questions the domain asks.** Forward iteration is strictly
exclusive, reverse iteration is strictly exclusive, and the domain needs an *inclusive* "at or
before" — so the adapter adds the inclusivity itself. That is a claim about this adapter, and these
tests are where it is checked.

**That the library's extra grammar stays unreachable.** `cronsim` accepts a sixth seconds field,
`L`, `LW` and `#`. The frozen NervOS dialect does not. The admission gate is the domain validator,
which runs before anything reaches this module, so the extras are refused by *NervOS* and never by
the library — and the tests below assert both halves of that, so neither can be quietly dropped.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from cronsim import CronSim, CronSimError
from nervos_core.domain.scheduling import ScheduleCalculationError, ScheduleFailure
from nervos_core.domain.triggers import CronExpression, InvalidTrigger
from nervos_core.infrastructure.scheduling import CronSimWallClock

WALL = CronSimWallClock()
NEW_YORK = ZoneInfo("America/New_York")
WEEKDAY = datetime(2026, 9, 18, 8, 0)  # a Friday


# ------------------------------------------------------------------------------------------------
# Forward and reverse iteration
# ------------------------------------------------------------------------------------------------


def test_forward_iteration_yields_the_next_match() -> None:
    assert WALL.next_after("0 9 * * *", WEEKDAY) == datetime(2026, 9, 18, 9, 0)


def test_forward_iteration_is_strictly_exclusive_of_its_argument() -> None:
    """The instant passed in is never returned, even when it is itself a match."""
    assert WALL.next_after("0 9 * * *", datetime(2026, 9, 18, 9, 0)) == datetime(2026, 9, 19, 9, 0)


def test_reverse_iteration_is_available_for_the_previous_match() -> None:
    assert WALL.at_or_before("0 9 * * *", datetime(2026, 9, 18, 9, 1)) == datetime(
        2026, 9, 18, 9, 0
    )


def test_at_or_before_is_inclusive_of_its_argument() -> None:
    """The library's reverse iteration is exclusive; the domain needs the exact boundary.

    Without the adapter's one-second step this answers the *previous* match, and the occurrence a
    due evaluation is trying to identify would be skipped over every time a schedule fired exactly
    on time.
    """
    assert WALL.at_or_before("0 9 * * *", datetime(2026, 9, 18, 9, 0)) == datetime(
        2026, 9, 18, 9, 0
    )
    assert WALL.at_or_before("0 9 * * *", datetime(2026, 9, 18, 8, 59, 59)) == datetime(
        2026, 9, 17, 9, 0
    )


def test_inclusivity_holds_one_second_each_side_of_a_match() -> None:
    match = datetime(2026, 9, 18, 9, 0)
    assert WALL.at_or_before("0 9 * * *", match - timedelta(seconds=1)) == match - timedelta(days=1)
    assert WALL.at_or_before("0 9 * * *", match) == match
    assert WALL.at_or_before("0 9 * * *", match + timedelta(seconds=1)) == match


def test_iteration_stays_in_naive_wall_clock_space() -> None:
    """The adapter converts no timezone, which keeps the DST contract NervOS's."""
    result = WALL.next_after("0 9 * * *", WEEKDAY)
    assert result is not None
    assert result.tzinfo is None


# ------------------------------------------------------------------------------------------------
# The parts of the dialect the library must be given unchanged
# ------------------------------------------------------------------------------------------------


def test_a_range_and_a_step_are_evaluated() -> None:
    assert WALL.next_after("0 9-17/4 * * *", WEEKDAY) == datetime(2026, 9, 18, 9, 0)
    assert WALL.next_after("0 10-17/4 * * *", WEEKDAY) == datetime(2026, 9, 18, 10, 0)


def test_a_list_is_evaluated() -> None:
    assert WALL.next_after("0 6,12,18 * * *", WEEKDAY) == datetime(2026, 9, 18, 12, 0)


def test_month_and_weekday_names_are_evaluated() -> None:
    assert WALL.next_after("0 9 * JAN *", WEEKDAY) == datetime(2027, 1, 1, 9, 0)
    # 2026-09-18 is a Friday, so 09:00 is still ahead of the 08:00 start.
    assert WALL.next_after("0 9 * * MON-FRI", WEEKDAY) == datetime(2026, 9, 18, 9, 0)
    # From later the same Friday, the next weekday match skips the weekend.
    assert WALL.next_after("0 9 * * MON-FRI", datetime(2026, 9, 18, 10, 0)) == datetime(
        2026, 9, 21, 9, 0
    )


def test_a_month_boundary_advances_the_year() -> None:
    assert WALL.next_after("0 9 1 JAN *", datetime(2026, 12, 31, 12, 0)) == datetime(
        2027, 1, 1, 9, 0
    )


# ------------------------------------------------------------------------------------------------
# Horizon and failure mapping
# ------------------------------------------------------------------------------------------------


def test_a_rare_but_real_expression_is_found_within_the_horizon() -> None:
    """`February 29` is valid; the horizon reaches the next one eight years out."""
    assert WALL.next_after("0 0 29 2 *", datetime(2026, 9, 18, 8, 0)) == datetime(2028, 2, 29, 0, 0)


def test_an_expression_with_no_occurrence_at_all_raises_the_typed_failure() -> None:
    """`April 31` parses field-by-field and can never match, so it fails at construction."""
    with pytest.raises(ScheduleCalculationError) as raised:
        WALL.next_after("0 0 31 4 *", WEEKDAY)
    assert raised.value.failure is ScheduleFailure.INVALID


def test_an_unparsable_expression_raises_the_typed_failure_not_the_library_error() -> None:
    with pytest.raises(ScheduleCalculationError) as raised:
        WALL.next_after("not a schedule", WEEKDAY)
    assert raised.value.failure is ScheduleFailure.INVALID


def test_the_adapter_never_propagates_a_library_exception() -> None:
    """Every library failure shape leaves through the frozen taxonomy, never as `CronSimError`."""
    for expression in ("bogus", "0 9 * *", "60 9 * * *", "0 0 31 4 *"):
        with pytest.raises(ScheduleCalculationError):
            WALL.next_after(expression, WEEKDAY)


# ------------------------------------------------------------------------------------------------
# The library's extra grammar stays unreachable
# ------------------------------------------------------------------------------------------------


def test_the_library_accepts_grammar_the_frozen_dialect_forbids() -> None:
    """Recorded on purpose, so the reason the domain validator exists cannot be forgotten.

    If a future library release stopped accepting these, this test would fail and the guard in
    `test_boundaries.py` would need revisiting — which is exactly the signal we want.
    """
    for expression in ("0 0 9 * * *", "0 0 L * *", "0 0 * * 5L", "0 0 * * 5#2"):
        assert WALL.next_after(expression, WEEKDAY) is not None


def test_nervos_refuses_every_extension_before_the_adapter_is_reached() -> None:
    """The admission gate is the domain validator, and it runs first."""
    for expression in ("0 0 9 * * *", "0 0 L * *", "0 0 * * 5L", "0 0 * * 5#2", "0 0 * * 5? "):
        with pytest.raises(InvalidTrigger):
            CronExpression(expression)


def test_the_library_is_reachable_only_through_the_adapter() -> None:
    """A direct construction, bypassing the adapter, is what the design forbids."""
    with pytest.raises(CronSimError):
        CronSim("60 9 * * *", WEEKDAY)
