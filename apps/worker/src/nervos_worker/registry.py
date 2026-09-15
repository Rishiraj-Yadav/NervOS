"""C3 Worker registry: durable process-incarnation liveness for one Worker process.

A registry row is **observability, not authority**. It records that an incarnation existed and
when it was last seen, so an operator can tell a stopped Worker from a crashed one. It never
reclaims a Job: expired-lease reclamation reads only Job leases and exact claim tuples.

The heartbeat is monotonic. A backwards wall clock must never make a healthy process look older,
so the durable write refuses to move `last_heartbeat_at` backwards, and a zero-row result is
resolved by reading the row back rather than guessed at.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from nervos_core.application.lease_reclamation import (
    LeaseReclaimer,
    ReclaimedClaim,
    WorkerLiveness,
    WorkerRegistryPersistence,
    WorkerSnapshot,
    WorkerState,
)

logger = logging.getLogger("nervos_worker.registry")

# Internal constants, per the C0 rule that an exposed setting needs a demonstrated operator
# requirement. Three renewal opportunities inside one stale window, mirroring the Job lease.
WORKER_HEARTBEAT_INTERVAL = timedelta(seconds=20)
WORKER_STALE_AFTER = timedelta(seconds=60)
# How often a Worker sweeps for expired claims once it is running.
RECLAIM_INTERVAL = timedelta(seconds=30)
# Bound on the single startup pass, so a large backlog cannot delay readiness indefinitely.
STARTUP_RECLAIM_LIMIT = 50

assert WORKER_STALE_AFTER >= 3 * WORKER_HEARTBEAT_INTERVAL


class WorkerRegistry:
    """Durable registration and liveness for exactly one process incarnation."""

    def __init__(
        self,
        persistence: WorkerRegistryPersistence,
        worker_id: str,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._persistence = persistence
        self.worker_id = worker_id
        self._clock = clock

    def register(self) -> datetime:
        """Insert this incarnation's row. A restart draws a new id and therefore a new row."""
        now = self._clock()
        self._persistence.register_worker(worker_id=self.worker_id, now=now)
        return now

    def heartbeat(self) -> WorkerLiveness:
        """Renew liveness once.

        `UNREGISTERED` and `STOPPED` contradict this Worker's own invariant that every executing
        incarnation is durably registered, so the caller must stop claiming. `REGRESSED` is
        benign: the clock moved backwards and nothing was lost.
        """
        return self._persistence.heartbeat_worker(worker_id=self.worker_id, now=self._clock())

    def stop(self) -> bool:
        """Record a graceful stop, mirroring the final heartbeat."""
        stopped = self._persistence.stop_worker(worker_id=self.worker_id, now=self._clock())
        logger.info("worker_registry_stopped worker_id=%s recorded=%s", self.worker_id, stopped)
        return stopped

    def snapshots(self) -> list[WorkerSnapshot]:
        """Report registry health. Read-only."""
        return list(
            self._persistence.list_workers(now=self._clock(), stale_after=WORKER_STALE_AFTER)
        )

    def log_summary(self) -> None:
        """Log derived health counts. No worker id, host, or credential is logged."""
        counts = {state.value: 0 for state in WorkerState}
        for snapshot in self.snapshots():
            counts[snapshot.state.value] += 1
        logger.info(
            "worker_registry_health healthy=%s stale=%s stopped=%s self=%s",
            counts[WorkerState.HEALTHY.value],
            counts[WorkerState.STALE.value],
            counts[WorkerState.STOPPED.value],
            self.worker_id,
        )


class ReclaimLoop:
    """Bounded expired-lease reclamation, safe to run concurrently without leader election."""

    def __init__(self, reclaimer: LeaseReclaimer, *, clock: Callable[[], datetime]) -> None:
        self._reclaimer = reclaimer
        self._clock = clock

    def sweep_once(self) -> ReclaimedClaim | None:
        """Reconcile at most one expired claim, or nothing when none is eligible."""
        reclaimed = self._reclaimer.reclaim_expired(now=self._clock())
        if reclaimed is not None:
            logger.info(
                "claim_reclaimed kind=%s run_id=%s job_id=%s attempt_id=%s attempt_number=%s",
                reclaimed.kind.value,
                reclaimed.run_id,
                reclaimed.job_id,
                reclaimed.attempt_id,
                reclaimed.attempt_number,
            )
        return reclaimed

    def startup_pass(self) -> list[ReclaimedClaim]:
        """One bounded pass at startup, so a restarted Worker drains stale work promptly.

        This performs no provider call and depends on no configured provider, so a Worker with no
        credential at all still reconciles.
        """
        reclaimed = self._reclaimer.reclaim_until_drained(
            now=self._clock(), limit=STARTUP_RECLAIM_LIMIT
        )
        if reclaimed:
            logger.info("startup_reclamation_complete reclaimed=%s", len(reclaimed))
        return reclaimed


__all__ = [
    "RECLAIM_INTERVAL",
    "STARTUP_RECLAIM_LIMIT",
    "WORKER_HEARTBEAT_INTERVAL",
    "WORKER_STALE_AFTER",
    "ReclaimLoop",
    "WorkerRegistry",
]
