"""The scheduler service: which due schedule becomes an ordinary Run, and nothing else.

This is the whole of Stage E's active behaviour. On each poll the process asks for a bounded page
of due schedules, decides for each one whether an occurrence is owed, and hands that decision to a
transaction that verifies it against durable state before applying it.

What it deliberately does **not** do is the point of the boundary. It never calls a model, never
executes a tool, never speaks MCP, never claims a Job, never creates an Attempt, never retries a
Run, never terminalizes one, and never evaluates a grant. A Run it creates is an ordinary Run: the
same submission primitive, the same queue, the same admission control, the same grant cutoff, and
the same D2 live authorization at dispatch as one a person submitted. Stage E decides *when* a Run
exists; Stages C and D continue to decide *how* it executes.

Two properties make a tick safe to run beside another copy of itself, and neither of them is a
lock:

* every decision carries the durable state it was computed from, and the transaction refuses it if
  that state has moved — so a losing scheduler writes nothing rather than writing a second time;
* every occurrence has a deterministic identity, so a duplicate decision resolves to the occurrence
  that already exists rather than creating another.

There is no leader election, no lease, no advisory lock and no coordination table. SQLite's write
lock decides which of two racing schedulers proceeds, and the database's own facts decide what the
loser does.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from nervos_core.application.agent_definitions import AgentDefinitionResolver
from nervos_core.application.clock import require_utc
from nervos_core.application.errors import PersistenceContention, QueueCapacityExceeded
from nervos_core.application.scheduling import ScheduleEvaluator
from nervos_core.application.triggers import (
    DueScheduleCandidate,
    ResolvedAgentDefinition,
    ScheduleMaterializationCommand,
    ScheduleMaterializationOutcome,
    ScheduleOutcomeKind,
    SchedulerPersistence,
    StaleReason,
    TriggerNotFound,
    resolve_agent_definition,
)
from nervos_core.domain.scheduling import ScheduleCalculationError, ScheduleFailure, schedule_of
from nervos_core.domain.triggers import SkipReason

#: How many due schedules one tick examines. Bounded so a backlog cannot turn a single tick into an
#: unbounded scan, and so the write lock is never held across a loop.
SCHEDULER_DUE_BATCH = 32

#: How long the process waits between ticks. Fixed, with no escalating idle backoff: a longer idle
#: interval would delay a newly created schedule by that much before its first check, trading a
#: real latency guarantee for a small amount of idle database work.
SCHEDULER_POLL_INTERVAL_SECONDS = 5.0

_COUNTERS = ("materialized", "skipped", "duplicated", "stale", "deferred_capacity")

_COUNTER_BY_OUTCOME: dict[ScheduleOutcomeKind, str] = {
    ScheduleOutcomeKind.MATERIALIZED: "materialized",
    ScheduleOutcomeKind.SKIPPED: "skipped",
    ScheduleOutcomeKind.DUPLICATED: "duplicated",
    ScheduleOutcomeKind.STALE: "stale",
    ScheduleOutcomeKind.DEFERRED_CAPACITY: "deferred_capacity",
}


@dataclass(frozen=True, slots=True)
class SchedulerTick:
    """One tick's complete, honest account of what it did.

    ``examined`` always equals the sum of every outcome counter, asserted here, because a counter
    list that silently stops adding up is how a scheduler reports healthy while doing nothing. Only
    ``materialized`` and ``skipped`` wrote a durable row; ``duplicated`` and ``stale`` are the races
    that correctly wrote nothing.
    """

    examined: int
    materialized: int
    skipped: int
    duplicated: int
    stale: int
    deferred_capacity: int
    deferred_contention: int

    def __post_init__(self) -> None:
        if self.examined != self._accounted():
            raise ValueError("a scheduler tick's counters must account for every candidate")

    def _accounted(self) -> int:
        return (
            self.materialized
            + self.skipped
            + self.duplicated
            + self.stale
            + self.deferred_capacity
            + self.deferred_contention
        )

    @property
    def effect_count(self) -> int:
        """Occurrences actually recorded. The two outcomes that wrote nothing are excluded."""
        return self.materialized + self.skipped

    @property
    def deferred(self) -> bool:
        """Whether any candidate was left for a later tick rather than resolved."""
        return bool(self.deferred_capacity or self.deferred_contention)


class SchedulerService:
    """Decide which due schedules become Runs. Owns no clock, no transaction and no execution."""

    def __init__(
        self,
        definitions: AgentDefinitionResolver,
        persistence: SchedulerPersistence,
        evaluator: ScheduleEvaluator,
        *,
        batch: int = SCHEDULER_DUE_BATCH,
    ) -> None:
        if batch <= 0:
            raise ValueError("the scheduler batch must be positive")
        self._definitions = definitions
        self._persistence = persistence
        self._evaluator = evaluator
        self._batch = batch
        # Process-local scan state only. It exists so a page that stays blocked by admission
        # backpressure cannot hide later due schedules; correctness never depends on it, because
        # every candidate is re-verified inside the transaction that acts on it. A restart resets
        # it, and two schedulers hold independent copies — neither changes any outcome.
        self._cursor: tuple[datetime, int] | None = None

    @property
    def cursor(self) -> tuple[datetime, int] | None:
        """The resume point of the last scan. Exposed for tests; never scheduling authority."""
        return self._cursor

    def tick(self, now: datetime) -> SchedulerTick:
        """Examine at most one page of due schedules and apply each decision atomically.

        `now` is a parameter rather than a clock read, so the same durable state and the same
        instant always produce the same tick and no test ever waits for time to pass.
        """
        moment = require_utc(now)
        candidates = self._persistence.due_schedule_candidates(
            now=moment, limit=self._batch, after=self._cursor
        )
        counters: dict[str, int] = dict.fromkeys(_COUNTERS, 0)
        deferred_contention = 0
        examined = 0
        for candidate in candidates:
            examined += 1
            try:
                outcome = self._resolve(candidate, moment)
            except PersistenceContention:
                # The shared retry budget is exhausted. Ending the tick is the only sound response:
                # continuing would immediately contend for the very write lock that was just not
                # obtained, and the fixed poll interval is already the retry schedule.
                deferred_contention = 1
                break
            counters[_COUNTER_BY_OUTCOME[outcome.kind]] += 1
        self._advance_cursor(candidates, examined)
        return SchedulerTick(
            examined=examined,
            materialized=counters["materialized"],
            skipped=counters["skipped"],
            duplicated=counters["duplicated"],
            stale=counters["stale"],
            deferred_capacity=counters["deferred_capacity"],
            deferred_contention=deferred_contention,
        )

    def _advance_cursor(self, candidates: tuple[DueScheduleCandidate, ...], examined: int) -> None:
        """Move the scan resume point, or wrap it, without ever making it authoritative.

        A full page advances to the last candidate so the next tick reaches rows this one did not.
        A short page means the scan reached the end of the due set, and wrapping to the start is
        what makes the cursor *fair* rather than merely monotonic: schedules deferred by admission
        backpressure are reconsidered instead of being stranded behind a page that never shrinks. A
        tick that ended early on contention leaves the cursor untouched, so the page it did not
        finish is re-read rather than stepped over.
        """
        if examined < len(candidates):
            return
        if len(candidates) == self._batch:
            last = candidates[-1]
            self._cursor = (last.expected_next_fire_at, last.trigger.id)
        else:
            self._cursor = None

    def _resolve(
        self, candidate: DueScheduleCandidate, now: datetime
    ) -> ScheduleMaterializationOutcome:
        """Evaluate one due schedule and apply the result, or record why nothing was applied."""
        trigger = candidate.trigger
        definition = resolve_agent_definition(self._definitions, trigger)
        try:
            # Rebuilding the schedule re-runs the frozen validation, so a stored row that has
            # become unrepresentable is reported here rather than evaluated on a guess.
            schedule = schedule_of(trigger)
            if schedule is None:
                # The scan selects schedule kinds by their next fire time, so a row whose schedule
                # is no longer representable can only mean the configuration changed underneath it.
                return ScheduleMaterializationOutcome(
                    kind=ScheduleOutcomeKind.STALE, stale_reason=StaleReason.NOT_SCHEDULE
                )
            decision = self._evaluator.decide_due(
                schedule, current_next_fire_at=candidate.expected_next_fire_at, now=now
            )
        except ScheduleCalculationError as error:
            return self._record_terminal_skip(candidate, definition, error, now)
        return self._apply(
            ScheduleMaterializationCommand(
                trigger_definition_id=trigger.id,
                definition=definition,
                now=now,
                occurred_at=now,
                expected_config_revision=candidate.expected_config_revision,
                expected_next_fire_at=candidate.expected_next_fire_at,
                nominal_at=decision.nominal_at,
                next_fire_at_after=decision.next_fire_at,
            )
        )

    def _record_terminal_skip(
        self,
        candidate: DueScheduleCandidate,
        definition: ResolvedAgentDefinition,
        error: ScheduleCalculationError,
        now: datetime,
    ) -> ScheduleMaterializationOutcome:
        """Record an unevaluable schedule as a skipped occurrence and disable the trigger.

        The occurrence's identity is the instant the durable row already names: the schedule *was*
        due, and this is the occurrence being answered. Deriving a new instant here would be
        impossible as well as wrong, since deriving instants is precisely what failed.

        The two failure categories are kept apart because they mean different things to whoever has
        to fix them — an unusable host timezone database is a deployment fact, an unusable schedule
        is a fact about the trigger — and each has its own frozen skip reason.
        """
        reason = (
            SkipReason.SCHEDULE_TIMEZONE_INVALID
            if error.failure is ScheduleFailure.TIMEZONE
            else SkipReason.SCHEDULE_INVALID
        )
        return self._apply(
            ScheduleMaterializationCommand(
                trigger_definition_id=candidate.trigger.id,
                definition=definition,
                now=now,
                occurred_at=now,
                expected_config_revision=candidate.expected_config_revision,
                expected_next_fire_at=candidate.expected_next_fire_at,
                nominal_at=candidate.expected_next_fire_at,
                next_fire_at_after=None,
                terminal_skip=reason,
            )
        )

    def _apply(self, command: ScheduleMaterializationCommand) -> ScheduleMaterializationOutcome:
        """Apply one decision, translating the two races into the outcomes they are.

        A trigger deleted between the scan and the transaction is a race, not a failure: the row is
        gone, nothing should be created for it, and the next tick has nothing to re-evaluate.

        Admission backpressure is the other kind of non-event. The transaction raised and rolled
        back **whole**, so the occurrence is still owed and the schedule is still due; classifying
        it as a deferral is what stops a busy queue from being recorded as a skipped automation.
        """
        try:
            return self._persistence.materialize_schedule_occurrence(command)
        except TriggerNotFound:
            return ScheduleMaterializationOutcome(
                kind=ScheduleOutcomeKind.STALE, stale_reason=StaleReason.DELETED
            )
        except QueueCapacityExceeded:
            return ScheduleMaterializationOutcome(kind=ScheduleOutcomeKind.DEFERRED_CAPACITY)


__all__ = [
    "SCHEDULER_DUE_BATCH",
    "SCHEDULER_POLL_INTERVAL_SECONDS",
    "SchedulerService",
    "SchedulerTick",
]
