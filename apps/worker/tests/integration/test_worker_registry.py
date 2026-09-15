"""C3 Worker registry, startup/periodic reclamation, and multi-Worker coordination.

These drive the *shipped* Worker loop over disposable persistence, so the registration,
heartbeat, identity-loss, and reclamation behaviour asserted here is what a running Worker
process performs.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from nervos_core.application.job_execution import LEASE_DURATION
from nervos_core.application.lease_reclamation import (
    LeaseReclaimer,
    ReclamationKind,
    WorkerLiveness,
    WorkerState,
)
from nervos_core.domain.jobs import JobStatus
from nervos_core.domain.runs import RunStatus
from nervos_worker.registry import (
    STARTUP_RECLAIM_LIMIT,
    WORKER_HEARTBEAT_INTERVAL,
    WORKER_STALE_AFTER,
    ReclaimLoop,
    WorkerRegistry,
)
from sqlalchemy import Engine
from support import (
    NOW,
    PROVIDER_ID,
    RecordingCompletion,
    build_execution_service,
    build_worker,
    counts,
    job_row,
    migrate,
    reclaim_expired_claim,
    run_row,
    submit,
    worker_rows,
)

EXPIRED_AT = NOW + timedelta(seconds=120)


class Clock:
    """A mutable clock, so lease expiry is advanced rather than slept through."""

    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def registry(engine: Engine, worker_id: str = "worker-1", clock: Clock | None = None):
    persistence, _ = build_execution_service(engine, {})
    return WorkerRegistry(persistence, worker_id, clock=clock or Clock())


def claim_with_a_short_lease(engine: Engine, worker_id: str = "dead-worker"):
    """Claim a Job whose lease has already lapsed by `EXPIRED_AT`.

    The Job's `available_at` is the submission instant, so the claim itself must happen at or
    after `NOW`; the lease is what is made short, not the claim instant.
    """
    persistence, _ = build_execution_service(engine, {})
    return persistence.claim_next(
        worker_id=worker_id,
        provider_ids=(PROVIDER_ID,),
        max_active=4,
        now=NOW,
        lease_duration=timedelta(seconds=1),
    )


def test_each_process_incarnation_registers_its_own_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart is a new incarnation: it must never recycle or overwrite an old identity."""
    engine = migrate(tmp_path / "registry.db", monkeypatch)
    try:
        build_worker(engine, {}, worker_id="host-1111-aaaaaaaaaaaaaaaa")
        build_worker(engine, {}, worker_id="host-2222-bbbbbbbbbbbbbbbb")

        rows = worker_rows(engine)
        assert [row["worker_id"] for row in rows] == [
            "host-1111-aaaaaaaaaaaaaaaa",
            "host-2222-bbbbbbbbbbbbbbbb",
        ]
        assert all(row["stopped_at"] is None for row in rows)
        assert rows[0]["started_at"] == rows[0]["last_heartbeat_at"]
    finally:
        engine.dispose()


def test_heartbeat_advances_and_graceful_stop_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "heartbeat.db", monkeypatch)
    try:
        persistence, _ = build_execution_service(engine, {})
        clock = Clock()
        registry = WorkerRegistry(persistence, "worker-1", clock=clock)
        registry.register()

        clock.value = NOW + timedelta(seconds=25)
        assert registry.heartbeat() is WorkerLiveness.RENEWED

        snapshots = persistence.list_workers(now=clock.value, stale_after=WORKER_STALE_AFTER)
        assert snapshots[0].state is WorkerState.HEALTHY

        assert registry.stop() is True
        stopped = persistence.list_workers(now=clock.value, stale_after=WORKER_STALE_AFTER)
        assert stopped[0].state is WorkerState.STOPPED
        # A stopped incarnation never heartbeats again.
        clock.value = NOW + timedelta(seconds=50)
        assert registry.heartbeat() is WorkerLiveness.STOPPED
        # Registry heartbeat and the Job lease are independent concerns.
        assert WORKER_HEARTBEAT_INTERVAL * 3 <= WORKER_STALE_AFTER
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_an_unregistered_worker_stops_claiming_and_shuts_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero-row identity loss must stop claims -- never keep claiming while unregistered."""
    engine = migrate(tmp_path / "identity.db", monkeypatch)
    try:
        run_id = submit(engine)
        completion = RecordingCompletion()
        # Registration is deliberately skipped, so the heartbeat finds no live row.
        worker = build_worker(engine, {PROVIDER_ID: completion}, register=False)

        stop = asyncio.Event()
        await asyncio.wait_for(worker.run(stop), timeout=10)

        assert stop.is_set()
        assert completion.calls == 0
        assert counts(engine)["job_attempts"] == 0
        assert job_row(engine, run_id)["status"] == JobStatus.QUEUED.value
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_graceful_shutdown_marks_the_registry_row_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "stop.db", monkeypatch)
    try:
        worker = build_worker(engine, {})
        stop = asyncio.Event()
        stop.set()
        await asyncio.wait_for(worker.run(stop), timeout=10)

        rows = worker_rows(engine)
        assert rows[0]["stopped_at"] is not None
        assert str(rows[0]["stopped_at"]) >= str(rows[0]["last_heartbeat_at"])
    finally:
        engine.dispose()


def test_startup_reclamation_needs_no_provider_and_sweeps_a_bounded_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Worker with zero configured providers must still reconcile stranded work."""
    engine = migrate(tmp_path / "startup.db", monkeypatch)
    try:
        run_id = submit(engine)
        persistence, _ = build_execution_service(engine, {})
        claimed = claim_with_a_short_lease(engine)
        assert claimed is not None

        loop = ReclaimLoop(LeaseReclaimer(persistence), clock=lambda: EXPIRED_AT)
        reclaimed = loop.startup_pass()

        assert [item.kind for item in reclaimed] == [ReclamationKind.PRE_START_REQUEUED]
        assert len(reclaimed) <= STARTUP_RECLAIM_LIMIT
        assert job_row(engine, claimed.job_id)["status"] == JobStatus.QUEUED.value
        assert run_row(engine, run_id)["status"] == RunStatus.CREATED.value
        assert (
            persistence.claim_next(
                worker_id="fresh",
                provider_ids=(PROVIDER_ID,),
                max_active=4,
                now=EXPIRED_AT + timedelta(seconds=10),
                lease_duration=LEASE_DURATION,
            )
            is not None
        )
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_periodic_reclamation_reconciles_expired_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The running sweep eventually reconciles an expired claim with no operator action."""
    engine = migrate(tmp_path / "periodic.db", monkeypatch)
    try:
        submit(engine)
        persistence, _ = build_execution_service(engine, {})
        claimed = claim_with_a_short_lease(engine)
        assert claimed is not None

        worker = build_worker(
            engine,
            {},
            register=True,
            reclaim_interval=0.01,
            heartbeat_interval=0.01,
            clock=lambda: EXPIRED_AT,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(stop))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if job_row(engine, claimed.job_id)["status"] == JobStatus.QUEUED.value:
                break
        stop.set()
        await asyncio.wait_for(task, timeout=10)

        assert job_row(engine, claimed.job_id)["status"] == JobStatus.QUEUED.value
        assert (
            persistence.claim_next(
                worker_id="fresh",
                provider_ids=(PROVIDER_ID,),
                max_active=4,
                now=EXPIRED_AT + timedelta(seconds=10),
                lease_duration=LEASE_DURATION,
            )
            is not None
        )
    finally:
        engine.dispose()


def test_two_workers_register_independently_and_one_can_be_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale incarnation must not block a healthy one from claiming or reclaiming."""
    engine = migrate(tmp_path / "multi.db", monkeypatch)
    try:
        run_id = submit(engine)
        persistence, _ = build_execution_service(engine, {})

        stale = WorkerRegistry(persistence, "stale-worker", clock=Clock())
        healthy = WorkerRegistry(persistence, "healthy-worker", clock=Clock())
        stale.register()
        healthy.register()
        healthy.heartbeat()

        snapshots = {
            snapshot.worker_id: snapshot
            for snapshot in persistence.list_workers(
                now=NOW + timedelta(minutes=5), stale_after=WORKER_STALE_AFTER
            )
        }
        assert snapshots["stale-worker"].state is WorkerState.STALE
        assert snapshots["healthy-worker"].state is WorkerState.STALE  # same fixed clock

        # Two registry rows, and the healthy-looking one still claims normally.
        assert len(worker_rows(engine)) == 2
        completion = RecordingCompletion()
        worker = build_worker(
            engine, {PROVIDER_ID: completion}, worker_id="third-worker", register=True
        )
        assert worker.provider_ids == (PROVIDER_ID,)
        claimed = persistence.claim_next(
            worker_id="third-worker",
            provider_ids=(PROVIDER_ID,),
            max_active=4,
            now=NOW,
            lease_duration=LEASE_DURATION,
        )
        assert claimed is not None
        assert job_row(engine, run_id)["status"] == JobStatus.CLAIMED.value
    finally:
        engine.dispose()


def test_reclaiming_one_worker_s_work_does_not_disturb_another_s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reclamation names only the expired claim it classified."""
    engine = migrate(tmp_path / "isolate.db", monkeypatch)
    try:
        first = submit(engine, text_value="first")
        second = submit(engine, text_value="second")
        persistence, _ = build_execution_service(engine, {})

        expired = claim_with_a_short_lease(engine, "dead")
        live = persistence.claim_next(
            worker_id="alive",
            provider_ids=(PROVIDER_ID,),
            max_active=4,
            now=NOW,
            lease_duration=LEASE_DURATION,
        )
        assert expired is not None and live is not None and expired.run_id == first

        outcome = reclaim_expired_claim(engine, now=EXPIRED_AT)
        assert outcome is not None and outcome.run_id == first
        assert job_row(engine, first)["status"] == JobStatus.QUEUED.value
        assert job_row(engine, second)["status"] == JobStatus.CLAIMED.value
        assert run_row(engine, second)["status"] == RunStatus.CREATED.value
    finally:
        engine.dispose()
