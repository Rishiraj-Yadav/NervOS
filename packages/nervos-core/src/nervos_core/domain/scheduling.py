"""Stage E schedule calculation: which instant a due schedule is due *for*, and when it is due next.

This module is provider-neutral and dependency-free. It imports no cron library and no database
type: it decides only *when*, and hands the answer to a caller that owns the transaction. The one
capability it cannot implement itself is iterating a cron expression, and that arrives through the
:class:`CronWallClock` port so the dialect and the arithmetic stay NervOS's while the iteration
stays a library's.

Three rules shape everything below, and all three are frozen by ADR 0018.

**Misfire is coalesced, not replayed.** A schedule that was missed many times produces exactly one
catch-up occurrence at the *latest* missed nominal instant, and then advances to the first
strictly-future one. A scheduler that has been down for a week must not produce a week of Runs.

**The nominal instant is the scheduled one, never the noticing one.** An occurrence caused by a
schedule due at 09:00 is due *for* 09:00, because that instant is the occurrence's identity: it is
what makes "was this schedule already honoured?" answerable, and what stops a restart, a second
scheduler or a long outage from producing the same occurrence twice.

**Wall-clock scheduling is resolved by NervOS, not by the library.** Cron is a statement about
local time, so evaluation runs on naive local readings and every local-to-UTC conversion happens
here. A zone's hour that does not exist, and its hour that happens twice, are therefore decided by
the contract below rather than by whichever behaviour a library happens to implement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nervos_core.domain.triggers import (
    DEFAULT_TIMEZONE,
    INTERVAL_MAX_SECONDS,
    INTERVAL_MIN_SECONDS,
    CronExpression,
    InvalidTrigger,
    ScheduleSpec,
    TimeZoneName,
    TriggerDefinition,
    TriggerKind,
)

#: The last instant a schedule calculation may ever produce.
#:
#: Interval arithmetic is O(1) and unbounded in principle, so a schedule whose next occurrence
#: lands past the calendar is refused as a typed calculation failure rather than being allowed to
#: raise an `OverflowError` out of a datetime constructor. Checking against this *before* the
#: arithmetic is what keeps the failure inside the frozen taxonomy.
MAX_SCHEDULE_HORIZON = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)

# Bisection steps needed to land on a zone transition. A real offset change is at most a day, so
# the search window is at most 86 400 seconds and converges in 17 steps; the bound is generous and
# exists so a malformed zone table can never turn the search into an unbounded loop.
_GAP_SEARCH_LIMIT = 64


class ScheduleFailure(StrEnum):
    """The two distinguishable reasons a schedule cannot be evaluated.

    They are separate because they mean different things to an operator: an unusable **timezone**
    is a deployment fact about the host's tz database, while an unusable **schedule** is a fact
    about the trigger's own configuration. Both end the same way, and each maps to its own frozen
    skip reason.
    """

    TIMEZONE = "timezone"
    INVALID = "invalid"


class ScheduleCalculationError(ValueError):
    """A schedule cannot be evaluated. Carries a static category and never library text.

    The message is composed from the enum alone. A `cronsim` or `zoneinfo` message is never
    interpolated here, because this error travels into a durable occurrence's static skip code and
    message and into the operator's log, and neither may carry library internals.
    """

    def __init__(self, failure: ScheduleFailure) -> None:
        super().__init__(f"schedule could not be evaluated: {failure.value}")
        self.failure = failure


class CronWallClock(Protocol):
    """Iteration over a cron expression in **naive local** wall-clock space.

    Both operations are strict: `next_after` excludes its argument, `at_or_before` includes it.
    The asymmetry is deliberate and is exactly what a due evaluation needs — the occurrence being
    honoured is the one at or before now, and the one being stored next is strictly after it.
    Implementations never resolve a timezone: a naive value in, a naive value out.
    """

    def next_after(self, expression: str, wall: datetime) -> datetime | None: ...

    def at_or_before(self, expression: str, wall: datetime) -> datetime | None: ...


@dataclass(frozen=True, slots=True)
class ScheduleDecision:
    """One due evaluation's complete answer, and the only thing a materialization may act on.

    ``nominal_at`` is the occurrence's **identity**. ``next_fire_at`` is the trigger's next state:
    `None` means this schedule is finished and the same transaction must disable it — the only
    shape the `next_fire_alignment` invariant accepts for a terminal transition.
    """

    nominal_at: datetime
    next_fire_at: datetime | None
    terminal: bool

    def __post_init__(self) -> None:
        nominal = _as_utc(self.nominal_at)
        object.__setattr__(self, "nominal_at", nominal)
        if self.next_fire_at is None:
            if not self.terminal:
                raise InvalidTrigger("a recurring decision requires the next fire time")
            return
        if self.terminal:
            raise InvalidTrigger("a terminal decision carries no next fire time")
        following = _as_utc(self.next_fire_at)
        object.__setattr__(self, "next_fire_at", following)
        if following <= nominal:
            raise InvalidTrigger("the next fire time must be after the nominal instant")


def calculate_one_time(next_fire_at: datetime, *, now: datetime) -> ScheduleDecision:
    """A one-time schedule is due exactly once, for the instant it names.

    Already-fired is not a state this can be asked about: the durable record of having fired is the
    occurrence, and the trigger is disabled in the same transaction that writes it. So the only
    question left is the identity, which is the stored instant itself.
    """
    instant = _require_due(next_fire_at, now)
    return ScheduleDecision(nominal_at=instant, next_fire_at=None, terminal=True)


def calculate_interval(
    next_fire_at: datetime, *, interval_seconds: int, now: datetime
) -> ScheduleDecision:
    """Fixed-duration scheduling, evaluated in O(1) however long the outage was.

    The stored instant is a point on an exact fixed-rate sequence, so the latest nominal instant at
    or before `now` is arithmetic rather than iteration: a three-day outage on a five-minute
    interval resolves in one division, not 864 steps. The advance lands strictly after `now` by
    construction of the floor, so a schedule can never be immediately due again.
    """
    if not INTERVAL_MIN_SECONDS <= interval_seconds <= INTERVAL_MAX_SECONDS:
        raise InvalidTrigger("interval is outside its frozen bounds")
    anchor = _require_due(next_fire_at, now)
    steps = int((now - anchor).total_seconds()) // interval_seconds
    elapsed = steps * interval_seconds
    # Checked before the arithmetic, so an impossible instant is a typed failure rather than an
    # `OverflowError` escaping a datetime constructor.
    budget = int((MAX_SCHEDULE_HORIZON - anchor).total_seconds())
    if elapsed > budget or elapsed + interval_seconds > budget:
        raise ScheduleCalculationError(ScheduleFailure.INVALID)
    nominal = anchor + timedelta(seconds=elapsed)
    return ScheduleDecision(
        nominal_at=nominal,
        next_fire_at=nominal + timedelta(seconds=interval_seconds),
        terminal=False,
    )


def calculate_cron(
    next_fire_at: datetime,
    *,
    expression: str,
    timezone: str,
    now: datetime,
    wall_clock: CronWallClock,
) -> ScheduleDecision:
    """Cron scheduling, evaluated on the zone's **local** wall clock.

    The stored instant is not used as the identity. It proves the trigger is due and it is the
    anchor a stale-write guard compares against, but after a long outage the occurrence must be the
    *latest* missed local match, not the oldest one — so the nominal instant is recomputed from the
    expression at or before `now`, and the next fire time is the first match strictly after it.
    """
    zone = zone_for(timezone)
    anchor = _require_due(next_fire_at, now)
    local_now = now.astimezone(zone).replace(tzinfo=None)
    latest = wall_clock.at_or_before(expression, local_now)
    following = wall_clock.next_after(expression, local_now)
    if latest is None or following is None:
        # The expression has no occurrence within the evaluator's horizon in either direction.
        raise ScheduleCalculationError(ScheduleFailure.INVALID)
    nominal = resolve_local(latest, zone)
    next_instant = resolve_local(following, zone)
    if nominal > anchor and nominal > now:
        # Unreachable for a well-behaved wall clock, and cheap to refuse: a nominal instant in the
        # future would consume an occurrence that has not happened.
        raise ScheduleCalculationError(ScheduleFailure.INVALID)
    if next_instant <= now:
        raise ScheduleCalculationError(ScheduleFailure.INVALID)
    return ScheduleDecision(nominal_at=nominal, next_fire_at=next_instant, terminal=False)


def initial_next_fire(
    schedule: ScheduleSpec, *, now: datetime, wall_clock: CronWallClock
) -> datetime:
    """The first instant a newly created or edited schedule should fire.

    This is the second half of the one reusable calculation seam. E2 uses it only in tests — the
    accepted E1 `next_fire_at` is still supplied by the caller — but it lives here so the trigger
    management surface of a later milestone cannot grow a second, divergent cron implementation.

    An interval starts one whole period from now, and a cron starts at its first match strictly
    after now, so a schedule created at 09:30 with an hourly expression fires at 10:00 rather than
    immediately. A one-time schedule keeps the instant it was given, even if that is already past:
    a schedule that names an instant in the past is due immediately, and "due immediately" is
    expressed by storing that instant, not by moving it.
    """
    instant = _as_utc(now)
    kind = schedule.kind
    if kind is TriggerKind.ONE_TIME:
        if schedule.run_at is None:
            raise InvalidTrigger("a one-time schedule requires an instant")
        return _as_utc(schedule.run_at)
    if kind is TriggerKind.INTERVAL:
        seconds = schedule.interval_seconds
        if seconds is None:
            raise InvalidTrigger("an interval schedule requires a duration")
        following = instant + timedelta(seconds=seconds)
        if following > MAX_SCHEDULE_HORIZON:
            raise ScheduleCalculationError(ScheduleFailure.INVALID)
        return following
    if kind is not TriggerKind.CRON:
        raise InvalidTrigger("a schedule spec is only for a schedule kind")
    if schedule.cron_expression is None:
        raise InvalidTrigger("a cron schedule requires an expression")
    zone = zone_for(schedule.timezone.name if schedule.timezone is not None else "")
    local_now = instant.astimezone(zone).replace(tzinfo=None)
    following_wall = wall_clock.next_after(schedule.cron_expression.text, local_now)
    if following_wall is None:
        raise ScheduleCalculationError(ScheduleFailure.INVALID)
    return resolve_local(following_wall, zone)


def schedule_of(definition: TriggerDefinition) -> ScheduleSpec | None:
    """Rebuild the schedule a durable trigger carries, or `None` for a delivery-driven kind.

    Reconstructing rather than storing the spec means the stored columns stay the single authority
    and a spec can never drift from them. It also re-runs the frozen validation on every
    evaluation, so a row that somehow became unrepresentable is refused loudly instead of being
    evaluated on a guess.

    The two ways a stored cron row can be unrepresentable are checked **separately and in order**,
    because they are different facts about the deployment and each has its own frozen skip reason:
    an IANA name the host's tz database no longer supplies is a fact about the host, while
    expression text the dialect no longer accepts is a fact about the trigger. Letting the general
    constructor report both as one error would collapse that distinction at exactly the moment an
    operator needs it.
    """
    kind = definition.kind
    if kind is TriggerKind.ONE_TIME:
        return None if definition.run_at is None else ScheduleSpec.one_time(definition.run_at)
    if kind is TriggerKind.INTERVAL:
        seconds = definition.interval_seconds
        return None if seconds is None else ScheduleSpec.interval(seconds)
    if kind is TriggerKind.CRON:
        text = definition.cron_expression
        if text is None:
            return None
        timezone = definition.timezone or DEFAULT_TIMEZONE
        zone_for(timezone)
        try:
            expression = CronExpression(text)
        except InvalidTrigger as error:
            raise ScheduleCalculationError(ScheduleFailure.INVALID) from error
        return ScheduleSpec(kind=kind, cron_expression=expression, timezone=TimeZoneName(timezone))
    return None


def zone_for(timezone: str) -> ZoneInfo:
    """Resolve an IANA zone name, reporting a missing one as a schedule failure.

    Resolution happens again at every evaluation even though the name was resolved when it was
    entered, because the host's tz database is not part of the trigger: a name that resolved last
    year can be absent today.
    """
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ScheduleCalculationError(ScheduleFailure.TIMEZONE) from error


def resolve_local(wall: datetime, zone: ZoneInfo) -> datetime:
    """The absolute UTC instant one local wall-clock reading means, under the frozen DST contract.

    * **Unambiguous** — the single instant that reads as `wall`.
    * **Ambiguous** (fall back, so the reading happens twice) — the **first**, pre-transition
      occurrence. `fold=0` selects it, and the second pass is never produced because iteration
      happens in wall-clock space, where the repeated reading occurs once.
    * **Nonexistent** (spring forward, so the reading is skipped) — the **transition instant**, at
      the end of the gap.

    The nonexistent case is the one that cannot be read off a `fold` flag. For `America/New_York`
    on 2026-03-08, a target of local 02:30 gives 07:30 UTC with `fold=0` and 06:30 UTC with
    `fold=1`, while the contract requires 07:00 UTC — the moment the local clock reaches 03:00.
    Neither fold value is that instant, so the transition is located explicitly: it lies within
    `(fold1, fold0]` by construction, and bisection over that bounded window finds it. An
    unambiguous reading takes the early return and never pays for the search.
    """
    if wall.tzinfo is not None:
        raise InvalidTrigger("a wall-clock reading must be naive")
    candidate = wall.replace(tzinfo=zone)
    if candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == wall:
        return candidate.astimezone(UTC)
    return _gap_end(candidate, zone)


def _gap_end(candidate: datetime, zone: ZoneInfo) -> datetime:
    """The first UTC instant at the end of the spring-forward gap `candidate` falls inside.

    By PEP 495 a nonexistent reading interpreted with `fold=0` uses the offset in effect *before*
    the transition, and with `fold=1` the offset *after* it. Those two instants therefore bracket
    the transition, and because the two offsets differ by exactly the gap, the bracketing window is
    one gap wide — at most a few hours, and not assumed to be one hour. Bisecting for the first
    instant whose offset has changed lands on the transition itself.
    """
    before_gap = candidate.replace(fold=1).astimezone(UTC)
    after_gap = candidate.astimezone(UTC)
    if before_gap > after_gap:  # pragma: no cover - defensive; a gap always orders this way
        raise ScheduleCalculationError(ScheduleFailure.INVALID)
    low, high = before_gap, after_gap
    pre_transition = low.astimezone(zone).utcoffset()
    for _ in range(_GAP_SEARCH_LIMIT):
        remaining = int((high - low).total_seconds())
        if remaining <= 1:
            break
        middle = low + timedelta(seconds=remaining // 2)
        if middle.astimezone(zone).utcoffset() == pre_transition:
            low = middle
        else:
            high = middle
    return high


def _require_due(next_fire_at: datetime, now: datetime) -> datetime:
    """Normalize a stored instant and require it to be due, or refuse the calculation.

    The due scan already guarantees this, so a violation means the calculation is being asked about
    a schedule that is not due — an invariant failure, not an operator condition.
    """
    instant = _as_utc(next_fire_at)
    moment = _as_utc(now)
    if instant > moment:
        raise InvalidTrigger("a schedule is not due yet")
    return instant


def _as_utc(value: datetime) -> datetime:
    """Require an aware instant and normalize it to UTC.

    No schedule calculation accepts or produces a naive datetime: a naive instant has no meaning
    across a process boundary, and the frozen contract stores UTC.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidTrigger("a schedule instant must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "MAX_SCHEDULE_HORIZON",
    "CronWallClock",
    "ScheduleCalculationError",
    "ScheduleDecision",
    "ScheduleFailure",
    "calculate_cron",
    "calculate_interval",
    "calculate_one_time",
    "initial_next_fire",
    "resolve_local",
    "schedule_of",
    "zone_for",
]
