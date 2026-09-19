"""The one `cronsim` adapter, and the schedule evaluator that composes it.

Everything above this module — the schedule arithmetic, the misfire rule, the local-to-UTC
resolution — is NervOS's, and knows nothing about the library. This file is the only place in the
repository that imports it, which is what makes three properties true at once:

* the cron **dialect** stays NervOS's, because the library is never an input-validation surface;
* a second evaluator, or a different one entirely, is a change to one module;
* no domain and no application module can acquire a library dependency by accident.

The library is used for iteration and nothing else. It is handed a **naive local** reading and
returns naive local readings, so it never performs a timezone conversion and cannot disagree with
the frozen DST contract: the one hour a year that does not exist, and the one that happens twice,
are resolved in `nervos_core.domain.scheduling`, by NervOS.

The library accepts more than the frozen dialect does — a sixth seconds field, `L`, `LW` and `#`
are all valid to it. That is not a gap: the domain validator is the sole admission gate and runs
before anything reaches this module, so the library's extra grammar is unreachable rather than
merely unused.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from cronsim import CronSim, CronSimError

from nervos_core.application.scheduling import ScheduleEvaluator
from nervos_core.domain.scheduling import (
    CronWallClock,
    ScheduleCalculationError,
    ScheduleDecision,
    ScheduleFailure,
    calculate_cron,
    calculate_interval,
    calculate_one_time,
    initial_next_fire,
)
from nervos_core.domain.triggers import ScheduleSpec, TriggerKind

#: Granularity of the inclusive lookup. Matches are minute-resolution in the five-field dialect, so
#: stepping back by one second asks "was the instant itself a match?" without ever landing on a
#: different one.
_INCLUSIVE_STEP = timedelta(seconds=1)


class CronSimWallClock:
    """`CronWallClock` over `cronsim`, in naive local wall-clock space.

    Both directions are provided because a due evaluation needs both: the occurrence being honoured
    is the latest match at or before now, and the stored next fire time is the first match strictly
    after it. Only the second is a plain forward iteration.
    """

    def next_after(self, expression: str, wall: datetime) -> datetime | None:
        return self._iterate(expression, wall, reverse=False)

    def at_or_before(self, expression: str, wall: datetime) -> datetime | None:
        """The latest match at or before `wall`, inclusive of `wall` itself.

        The library's reverse iteration is strictly exclusive, so asking it directly would answer
        "the previous match" whenever `wall` is itself a match — which is precisely the instant a
        due evaluation is trying to identify. Stepping forward by one second first makes the strict
        iteration answer the inclusive question, and never skips a match, because the dialect's
        finest granularity is a minute.
        """
        return self._iterate(expression, wall + _INCLUSIVE_STEP, reverse=True)

    @staticmethod
    def _iterate(expression: str, wall: datetime, *, reverse: bool) -> datetime | None:
        """One step of iteration, mapping both library failure shapes onto the frozen taxonomy.

        An unparsable expression, an empty field range and a horizon exhausted before any match are
        all "this schedule cannot be evaluated". The library's own message is never propagated: it
        would reach a durable skip field and an operator log, and neither carries library text.
        """
        try:
            iterator = iter(CronSim(expression, wall, reverse=reverse))
            return next(iterator)
        except StopIteration:
            return None
        except CronSimError as error:
            raise ScheduleCalculationError(ScheduleFailure.INVALID) from error


class CronSimScheduleEvaluator:
    """The production schedule evaluator.

    It owns no clock and no state: every call receives the instant it is being asked about, so the
    same input always produces the same answer and a test never waits for a minute to pass.
    """

    def __init__(self, wall_clock: CronWallClock | None = None) -> None:
        self._wall_clock: CronWallClock = CronSimWallClock() if wall_clock is None else wall_clock

    def decide_due(
        self, schedule: ScheduleSpec, *, current_next_fire_at: datetime, now: datetime
    ) -> ScheduleDecision:
        kind = schedule.kind
        if kind is TriggerKind.ONE_TIME:
            return calculate_one_time(current_next_fire_at, now=now)
        if kind is TriggerKind.INTERVAL:
            seconds = schedule.interval_seconds
            if seconds is None:
                raise ScheduleCalculationError(ScheduleFailure.INVALID)
            return calculate_interval(current_next_fire_at, interval_seconds=seconds, now=now)
        if kind is TriggerKind.CRON:
            expression = schedule.cron_expression
            timezone = schedule.timezone
            if expression is None or timezone is None:
                raise ScheduleCalculationError(ScheduleFailure.INVALID)
            return calculate_cron(
                current_next_fire_at,
                expression=expression.text,
                timezone=timezone.name,
                now=now,
                wall_clock=self._wall_clock,
            )
        # A delivery-driven trigger is never due on a clock. Reaching here means a schedule
        # evaluation was attempted for a kind that has no schedule.
        raise ScheduleCalculationError(ScheduleFailure.INVALID)

    def initial_next_fire(self, schedule: ScheduleSpec, *, now: datetime) -> datetime:
        return initial_next_fire(schedule, now=now, wall_clock=self._wall_clock)


def create_schedule_evaluator() -> ScheduleEvaluator:
    """The evaluator this process should use. One construction, one import of the library."""
    return CronSimScheduleEvaluator()


__all__ = ["CronSimScheduleEvaluator", "CronSimWallClock", "create_schedule_evaluator"]
