"""The schedule evaluation port: one reusable seam for both directions of schedule arithmetic.

Two capabilities are needed across Stage E, and they belong to the same abstraction because a
second implementation of one without the other would let the two disagree:

* **deciding a due occurrence** — which nominal instant this catch-up is *for*, and what the next
  fire time becomes. E2's scheduler is the only caller today.
* **seeding a new schedule** — the first instant a newly created or edited schedule should fire.
  Nothing in E2 calls this; it exists so the trigger management surface of a later milestone does
  not have to grow its own cron implementation beside this one.

The port is provider-neutral: it names no cron library, no database type and no clock. An
implementation receives the instant it is being asked about on every call and returns a
:class:`~nervos_core.domain.scheduling.ScheduleDecision`, so evaluation is a pure function of
`(schedule, stored instant, now)` and never reads a clock of its own.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from nervos_core.domain.scheduling import ScheduleDecision
from nervos_core.domain.triggers import ScheduleSpec


class ScheduleEvaluator(Protocol):
    """Where a schedule's timing decisions come from."""

    def decide_due(
        self, schedule: ScheduleSpec, *, current_next_fire_at: datetime, now: datetime
    ) -> ScheduleDecision:
        """Decide the occurrence a due schedule is being honoured for.

        ``current_next_fire_at`` is the instant the durable row says is due, which is both the
        trigger's proof that it is due and the anchor a stale-write guard compares against. It is
        **not** necessarily the nominal instant: after a long outage the occurrence is the latest
        missed instant, not the oldest one.

        Raises :class:`~nervos_core.domain.scheduling.ScheduleCalculationError` when the schedule
        cannot be evaluated at all — an unusable timezone, or no occurrence within the evaluator's
        horizon. Both are terminal for the trigger and are recorded as a skipped occurrence.
        """
        ...

    def initial_next_fire(self, schedule: ScheduleSpec, *, now: datetime) -> datetime:
        """The first instant a newly created or edited schedule is due.

        Raises the same error as :meth:`decide_due` when the schedule admits no occurrence.
        """
        ...


__all__ = ["ScheduleEvaluator"]
