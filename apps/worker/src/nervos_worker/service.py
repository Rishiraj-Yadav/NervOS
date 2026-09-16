"""Stage C3 Worker: bounded execution slots, durable registry liveness, and reclamation.

The Worker owns no HTTP surface and never migrates the database. Each slot claims at most one
Job at a time, executes it through `JobExecutionService`, and returns to polling. Two auxiliary
tasks run beside the slots: a registry heartbeat that keeps this process incarnation observable,
and a periodic sweep that reconciles expired claims. Every blocking persistence call is
dispatched off the event loop, because the synchronous SQLite driver would otherwise freeze an
unrelated Job's heartbeat.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime

from nervos_core.application.clock import Clock, require_utc
from nervos_core.application.errors import PersistenceUnavailable
from nervos_core.application.job_execution import (
    LEASE_DURATION,
    ClaimedAttempt,
    JobExecutionPersistence,
    JobExecutionService,
)
from nervos_core.application.lease_reclamation import WorkerLiveness
from nervos_core.application.model_completion import ModelCompletion

from nervos_worker.registry import (
    RECLAIM_INTERVAL,
    WORKER_HEARTBEAT_INTERVAL,
    ReclaimLoop,
    WorkerRegistry,
)

logger = logging.getLogger("nervos_worker")

DEFAULT_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_IDLE_MAX_SECONDS = 2.0
DEFAULT_SHUTDOWN_GRACE_SECONDS = 10.0
# How long a slot that ignored its cancellation is awaited before it is left to finish on its
# own. Bounded so one non-cooperative provider coroutine cannot hang shutdown forever.
DEFAULT_SHUTDOWN_DRAIN_SECONDS = 5.0


def _discard_slot_outcome(task: asyncio.Task[None]) -> None:
    """Consume an abandoned slot's eventual outcome so it is never reported as unretrieved.

    The outcome is deliberately discarded: a slot that outlived its drain bound may still hold an
    expiring lease, and C3 -- not this callback -- owns whatever its claim reconciles to.
    """
    with contextlib.suppress(BaseException):
        task.exception()


class Worker:
    """Run `concurrency` execution slots until the stop event is set."""

    def __init__(
        self,
        persistence: JobExecutionPersistence,
        execution: JobExecutionService,
        completions: Mapping[str, ModelCompletion],
        *,
        clock: Clock,
        worker_id: str,
        concurrency: int,
        max_active: int,
        registry: WorkerRegistry,
        reclaimer: ReclaimLoop,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        idle_max: float = DEFAULT_IDLE_MAX_SECONDS,
        shutdown_grace: float = DEFAULT_SHUTDOWN_GRACE_SECONDS,
        shutdown_drain_seconds: float = DEFAULT_SHUTDOWN_DRAIN_SECONDS,
        heartbeat_interval: float = WORKER_HEARTBEAT_INTERVAL.total_seconds(),
        reclaim_interval: float = RECLAIM_INTERVAL.total_seconds(),
    ) -> None:
        if not 1 <= concurrency <= 16:
            raise ValueError("worker concurrency must be between 1 and 16")
        if not 1 <= max_active <= 16:
            raise ValueError("max active jobs must be between 1 and 16")
        self._persistence = persistence
        self._execution = execution
        self._provider_ids = tuple(sorted(completions))
        self._clock = clock
        self._worker_id = worker_id
        self._concurrency = concurrency
        self._max_active = max_active
        self._registry = registry
        self._reclaimer = reclaimer
        self._poll_interval = poll_interval
        self._idle_max = idle_max
        self._shutdown_grace = shutdown_grace
        self._shutdown_drain_seconds = shutdown_drain_seconds
        self._heartbeat_interval = heartbeat_interval
        self._reclaim_interval = reclaim_interval

    @property
    def provider_ids(self) -> tuple[str, ...]:
        """Return the frozen set of provider identifiers this Worker can execute."""
        return self._provider_ids

    @property
    def worker_id(self) -> str:
        return self._worker_id

    async def run(self, stop: asyncio.Event) -> None:
        """Run until `stop` is set, then drain within the shutdown grace."""
        # Confirm durable registration *before* any slot may claim. Doing this in the heartbeat
        # task instead would be a race: a slot could claim and execute during the first heartbeat
        # interval, which would let an unregistered incarnation do work.
        liveness = await asyncio.to_thread(self._registry.heartbeat)
        if liveness in (WorkerLiveness.UNREGISTERED, WorkerLiveness.STOPPED):
            logger.warning("registry_identity_missing worker_id=%s", self._worker_id)
            stop.set()
            return
        slots = [
            asyncio.create_task(self._slot(index, stop), name=f"nervos-worker-slot-{index}")
            for index in range(self._concurrency)
        ]
        auxiliary = [
            asyncio.create_task(
                self._registry_heartbeat(stop), name="nervos-worker-registry-heartbeat"
            ),
            asyncio.create_task(self._reclaim_periodically(stop), name="nervos-worker-reclaimer"),
        ]
        try:
            await self._wait_for_stop(stop, [*slots, *auxiliary])
        finally:
            await self._drain(slots)
            for task in auxiliary:
                task.cancel()
            for task in auxiliary:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await asyncio.to_thread(self._registry.stop)

    async def _registry_heartbeat(self, stop: asyncio.Event) -> None:
        """Renew registry liveness independently of every Job heartbeat.

        A transient persistence failure here is observability-only and must never revoke an
        active Job's authority: Job heartbeats continue on their own path and the lease remains
        the sole execution authority. A *successful* write that finds no live row for this
        incarnation is different: it contradicts the invariant that every executing Worker is
        durably registered, so this Worker stops accepting new claims and begins an orderly
        shutdown. In-flight work keeps its lease and follows the normal grace semantics.
        """
        while not stop.is_set():
            try:
                liveness = await asyncio.to_thread(self._registry.heartbeat)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("registry_heartbeat_deferred code=persistence_unavailable")
            else:
                if liveness in (WorkerLiveness.UNREGISTERED, WorkerLiveness.STOPPED):
                    logger.warning("registry_identity_lost worker_id=%s", self._worker_id)
                    stop.set()
                    return
            await self._idle(stop, self._heartbeat_interval)

    async def _reclaim_periodically(self, stop: asyncio.Event) -> None:
        """Sweep for expired claims on a bounded interval, one short transaction at a time."""
        while not stop.is_set():
            await self._idle(stop, self._reclaim_interval)
            if stop.is_set():
                return
            try:
                await asyncio.to_thread(self._reclaimer.sweep_once)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("reclamation_deferred code=internal_execution_error")

    async def _wait_for_stop(
        self, stop: asyncio.Event, slots: Sequence[asyncio.Task[None]]
    ) -> None:
        stop_task = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait({stop_task, *slots}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_task

    async def _drain(self, slots: Sequence[asyncio.Task[None]]) -> None:
        """Let in-flight work finish inside the grace window, then cancel what remains.

        A task cancelled by the deadline writes no terminal state: the Attempt stays `running`
        with an expiring lease and C3 reconciles it. C2 never converts a shutdown into a false
        failure, and it never claims the remote provider stopped processing a request it had
        already received.

        The follow-up wait is bounded too. A slot whose provider coroutine suppresses
        cancellation cannot be forcibly terminated, and awaiting it without a limit let one such
        task hang shutdown indefinitely. A slot that outlives the bound is left to finish on its
        own with its outcome consumed, so shutdown is finite without pretending the coroutine was
        killed -- and because cancellation is never written here, that slot still holds only an
        expiring lease for C3 to reconcile.
        """
        if not slots:
            return
        _done, pending = await asyncio.wait(slots, timeout=self._shutdown_grace)
        for task in pending:
            task.cancel()
        if pending:
            _settled, stubborn = await asyncio.wait(pending, timeout=self._shutdown_drain_seconds)
            for task in stubborn:
                logger.warning("slot_abandoned_at_shutdown code=internal_execution_error")
                task.add_done_callback(_discard_slot_outcome)
        for task in slots:
            if not task.done():
                continue
            error = None if task.cancelled() else task.exception()
            if error is not None:
                logger.warning("slot_exited_unexpectedly code=internal_execution_error")

    async def _slot(self, index: int, stop: asyncio.Event) -> None:
        logger.info("slot_started index=%s", index)
        delay = self._poll_interval
        while not stop.is_set():
            try:
                claimed = await self._claim()
            except asyncio.CancelledError:
                raise
            except PersistenceUnavailable:
                logger.warning("claim_deferred code=persistence_unavailable")
                claimed = None
            except Exception:
                logger.warning("claim_failed_safely code=internal_execution_error")
                claimed = None
            if claimed is None:
                await self._idle(stop, delay)
                delay = min(delay * 2, self._idle_max)
                continue
            delay = self._poll_interval
            try:
                await self._execution.execute(claimed)
            except asyncio.CancelledError:
                logger.warning(
                    "execution_cancelled run_id=%s job_id=%s attempt_id=%s"
                    " claim_left_for_recovery=true",
                    claimed.run_id,
                    claimed.job_id,
                    claimed.attempt_id,
                )
                raise
            except Exception:
                logger.warning(
                    "execution_failed_safely run_id=%s job_id=%s attempt_id=%s"
                    " code=internal_execution_error claim_left_for_recovery=true",
                    claimed.run_id,
                    claimed.job_id,
                    claimed.attempt_id,
                )
        logger.info("slot_stopped index=%s", index)

    async def _claim(self) -> ClaimedAttempt | None:
        return await asyncio.to_thread(
            self._persistence.claim_next,
            worker_id=self._worker_id,
            provider_ids=self._provider_ids,
            max_active=self._max_active,
            now=self._now(),
            lease_duration=LEASE_DURATION,
        )

    async def _idle(self, stop: asyncio.Event, delay: float) -> None:
        """Sleep with idle backoff, waking immediately when a stop is requested."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(stop.wait()), timeout=delay)

    def _now(self) -> datetime:
        return require_utc(self._clock())
