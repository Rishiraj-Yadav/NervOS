"""C3 lease reclamation: expired-claim recovery and Worker-registry health.

Two separate concerns live here, and the separation is the point:

* **Worker registry health** is *observability*. It answers "was this process incarnation seen
  recently?" and nothing else. It never authorizes, blocks, or performs any Job mutation.
* **Lease reclamation** is *execution authority*. A Job whose `lease_expires_at` has passed is
  reclaimed on the strength of the expired lease and the exact claim tuple alone -- never
  because a registry row looked stale.

The crash classification is conservative and driven only by the committed execution-start
boundary:

* `claimed` with `execution_started_at IS NULL` -- the boundary provably never committed, so the
  Job may safely return to the queue (or be closed if its claim budget is spent).
* `execution_started_at IS NOT NULL` -- a provider call may have been issued. The outcome is
  irreducibly AMBIGUOUS and is never replayed.

This module is provider-neutral and holds no session, transaction, or ORM record across a call.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

# Timing constants are internal, per the C0 rule that an exposed setting needs a demonstrated
# operator requirement. Only the Worker service reads them.

# A fixed infrastructure delay applied when a pre-start expired claim returns to the queue, so a
# Worker that crashes deterministically at the same point cannot be re-claimed in a hot loop and
# burn its whole budget in milliseconds. This is not the C4 retry engine: there is no policy, no
# per-code table, and no exponential schedule.
RECLAIM_BACKOFF = timedelta(seconds=5)

# One candidate per transaction. SQLite holds a global write lock, so a batch would only lengthen
# the critical section that every heartbeat and claim in the process must wait behind.
RECLAIM_BATCH = 1


class WorkerState(StrEnum):
    """Derived Worker-registry health. Never persisted; always computed from timestamps."""

    HEALTHY = "healthy"
    STALE = "stale"
    STOPPED = "stopped"


class WorkerLiveness(StrEnum):
    """The result of one registry heartbeat attempt."""

    RENEWED = "renewed"
    # The wall clock moved backwards. A healthy process must not be made to look older.
    REGRESSED = "regressed"
    STOPPED = "stopped"
    # The registry row is gone. This contradicts the invariant that every executing incarnation
    # is durably registered, so the Worker must stop claiming rather than continue unregistered.
    UNREGISTERED = "unregistered"


def classify_worker(
    *,
    stopped_at: datetime | None,
    last_heartbeat_at: datetime,
    now: datetime,
    stale_after: timedelta,
) -> WorkerState:
    """Derive health from timestamps alone.

    `stale` means "not observed recently" and never "dead": a crashed Worker and a long-paused
    one are indistinguishable, and both are reported stale.
    """
    if stopped_at is not None:
        return WorkerState.STOPPED
    if last_heartbeat_at > now - stale_after:
        return WorkerState.HEALTHY
    return WorkerState.STALE


class ReclamationKind(StrEnum):
    """Which durable transition a reclaimed claim produced."""

    # The execution-start boundary provably never committed and another claim is available.
    PRE_START_REQUEUED = "pre_start_requeued"
    # The boundary provably never committed and the claim budget is spent, so the Job and Run are
    # closed truthfully as failed without a start boundary.
    PRE_START_EXHAUSTED = "pre_start_exhausted"
    # The boundary committed, so a provider call may have been issued. Never replayed.
    POST_START_AMBIGUOUS = "post_start_ambiguous"


@dataclass(frozen=True, slots=True)
class ReclaimedClaim:
    """One expired active claim that was reconciled, with the transition that was applied."""

    kind: ReclamationKind
    run_id: int
    job_id: int
    attempt_id: int
    attempt_number: int


@dataclass(frozen=True, slots=True)
class WorkerSnapshot:
    """One registry row, with health derived at read time."""

    worker_id: str
    state: WorkerState
    started_at: datetime
    last_heartbeat_at: datetime
    stopped_at: datetime | None


class ClaimReclamationPersistence(Protocol):
    """The durable operations lease reclamation needs, and nothing more."""

    def reclaim_next_expired_claim(
        self, *, now: datetime, backoff: timedelta
    ) -> ReclaimedClaim | None: ...

    def list_workers(
        self, *, now: datetime, stale_after: timedelta
    ) -> Sequence[WorkerSnapshot]: ...


class WorkerRegistryPersistence(Protocol):
    """The durable registry writes one Worker process needs, and nothing more."""

    def register_worker(self, *, worker_id: str, now: datetime) -> None: ...

    def heartbeat_worker(self, *, worker_id: str, now: datetime) -> WorkerLiveness: ...

    def stop_worker(self, *, worker_id: str, now: datetime) -> bool: ...

    def list_workers(
        self, *, now: datetime, stale_after: timedelta
    ) -> Sequence[WorkerSnapshot]: ...


class LeaseReclaimer:
    """Reconcile expired claims, one short transaction at a time.

    Safe to run concurrently in any number of Worker processes with no leader election: every
    mutation is CAS-checked against the exact expired claim it classified, so a losing reclaimer
    rolls back having produced no side effect.
    """

    def __init__(self, persistence: ClaimReclamationPersistence) -> None:
        self._persistence = persistence

    def reclaim_expired(self, *, now: datetime) -> ReclaimedClaim | None:
        """Reconcile at most one expired claim, or return None when none is eligible."""
        return self._persistence.reclaim_next_expired_claim(now=now, backoff=RECLAIM_BACKOFF)

    def reclaim_until_drained(self, *, now: datetime, limit: int) -> list[ReclaimedClaim]:
        """Reconcile up to `limit` expired claims, as the startup pass does."""
        reclaimed: list[ReclaimedClaim] = []
        while len(reclaimed) < limit:
            claim = self.reclaim_expired(now=now)
            if claim is None:
                break
            reclaimed.append(claim)
        return reclaimed

    def workers(self, *, now: datetime, stale_after: timedelta) -> Sequence[WorkerSnapshot]:
        """Report registry health. Read-only: this never mutates a Job."""
        return self._persistence.list_workers(now=now, stale_after=stale_after)


__all__ = [
    "RECLAIM_BACKOFF",
    "RECLAIM_BATCH",
    "ClaimReclamationPersistence",
    "LeaseReclaimer",
    "ReclaimedClaim",
    "ReclamationKind",
    "WorkerLiveness",
    "WorkerSnapshot",
    "WorkerState",
    "classify_worker",
]
