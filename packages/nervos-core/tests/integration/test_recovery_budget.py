"""C3 mandatory pre-start recovery budget test.

Three consecutive crash-before-start cycles must exhaust `max_attempts` and close the Run
truthfully: `failed`, no `started_at`, no `elapsed_ms`, the exhausted code, exactly three
historical Attempts, zero provider calls, no fourth claim, and no permanently queued Job.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from execution_support import NOW, attempt_row, event_types, job_row, migrate, run_row
from nervos_core.application.job_execution import LEASE_DURATION
from nervos_core.application.lease_reclamation import (
    RECLAIM_BACKOFF,
    ReclamationKind,
    WorkerLiveness,
    WorkerState,
)
from nervos_core.application.model_completion import (
    EXECUTION_OUTCOME_AMBIGUOUS,
    safe_error_message,
)
from nervos_core.domain.jobs import AttemptStatus, JobStatus
from nervos_core.domain.runs import (
    STAGE_B_LIMITS,
    WORKER_RECOVERY_EXHAUSTED,
    RunStatus,
)
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobExecutionPersistence,
    SqlAlchemyJobPersistence,
)
from sqlalchemy import Engine, text

PROVIDER = "anthropic"
LEASE_EXPIRED = NOW + LEASE_DURATION + timedelta(seconds=1)


def prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    engine = migrate(tmp_path / "recovery.db", monkeypatch)
    SqlAlchemyJobPersistence(engine, max_pending=1000).submit(
        owner_user_id=1,
        agent_instance_id=1,
        input_text="hello",
        limits=STAGE_B_LIMITS,
        now=NOW,
    )
    return engine


def claim(engine: Engine, worker_id: str = "worker-1", *, now: datetime = NOW):
    claimed = SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None).claim_next(
        worker_id=worker_id,
        provider_ids=(PROVIDER,),
        max_active=4,
        now=now,
        lease_duration=LEASE_DURATION,
    )
    assert claimed is not None
    return claimed


def reclaim(engine: Engine, *, now: datetime = LEASE_EXPIRED):
    return SqlAlchemyJobExecutionPersistence(
        engine, sleep=lambda _: None
    ).reclaim_next_expired_claim(now=now, backoff=RECLAIM_BACKOFF)


def stored(value: datetime) -> str:
    """Render a timestamp the way SQLite stores it, so raw-SQL comparisons stay exact."""
    return value.astimezone(UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")


def test_three_pre_start_crashes_exhaust_the_budget_truthfully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = prepare(tmp_path, monkeypatch)
    try:
        run_id = 1
        job_id = int(job_row_of(engine, run_id))

        # Cycles 1 and 2: the boundary never committed and budget remains, so the Job requeues.
        # Each cycle advances the clock past the lease and then past the reclaim backoff, which is
        # what keeps a deterministic crasher from being re-claimed in a hot loop.
        cursor = NOW
        for attempt_number in (1, 2):
            claimed = claim(engine, worker_id=f"worker-{attempt_number}", now=cursor)
            assert claimed.attempt_number == attempt_number
            expired_at = cursor + LEASE_DURATION + timedelta(seconds=1)
            outcome = reclaim(engine, now=expired_at)
            assert outcome is not None and outcome.kind is ReclamationKind.PRE_START_REQUEUED
            assert job_row(engine, job_id)["status"] == JobStatus.QUEUED.value
            assert attempt_row(engine, claimed.attempt_id)["status"] == AttemptStatus.EXPIRED.value
            assert run_row(engine, run_id)["status"] == RunStatus.CREATED.value
            # Held back by the backoff: not claimable at the moment of requeue.
            assert (
                SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None).claim_next(
                    worker_id="too-early",
                    provider_ids=(PROVIDER,),
                    max_active=4,
                    now=expired_at,
                    lease_duration=LEASE_DURATION,
                )
                is None
            )
            cursor = expired_at + RECLAIM_BACKOFF + timedelta(seconds=1)

        # Cycle 3: the budget is spent, so the Job and Run close truthfully.
        claimed = claim(engine, worker_id="worker-3", now=cursor)
        assert claimed.attempt_number == 3
        outcome = reclaim(engine, now=cursor + LEASE_DURATION + timedelta(seconds=1))
        assert outcome is not None and outcome.kind is ReclamationKind.PRE_START_EXHAUSTED

        job = job_row(engine, job_id)
        assert job["status"] == JobStatus.FAILED.value
        assert job["attempt_count"] == 3
        assert job["max_attempts"] == 3
        assert job["error_code"] == WORKER_RECOVERY_EXHAUSTED
        assert job["claimed_by"] is None and job["claim_token"] is None
        assert job["lease_expires_at"] is None and job["last_heartbeat_at"] is None

        run = run_row(engine, run_id)
        assert run["status"] == RunStatus.FAILED.value
        assert run["started_at"] is None
        assert run["elapsed_ms"] is None
        assert run["output_text"] is None
        assert run["finish_reason"] is None
        assert run["error_code"] == WORKER_RECOVERY_EXHAUSTED
        assert run["error_message"] == safe_error_message(WORKER_RECOVERY_EXHAUSTED)

        # Three historical Attempts, numbered 1..3, all retained as evidence.
        with engine.connect() as connection:
            attempts = connection.execute(
                text(
                    "SELECT attempt_number, status, retry_disposition FROM job_attempts"
                    " ORDER BY attempt_number"
                )
            ).all()
        assert attempts == [
            (1, "expired", "SAFE_TO_RETRY"),
            (2, "expired", "SAFE_TO_RETRY"),
            (3, "expired", "SAFE_TO_RETRY"),
        ]

        # No fourth claim is possible, and no Job is left queued or occupying capacity.
        assert (
            SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None).claim_next(
                worker_id="worker-4",
                provider_ids=(PROVIDER,),
                max_active=4,
                now=LEASE_EXPIRED + LEASE_DURATION,
                lease_duration=LEASE_DURATION,
            )
            is None
        )
        with engine.connect() as connection:
            occupying = connection.scalar(
                text(
                    "SELECT count(*) FROM jobs"
                    " WHERE status IN ('queued','claimed','running','retry_wait')"
                )
            )
        assert occupying == 0

        # The timeline records exactly the two recoveries' events plus the exhausted closeout.
        assert event_types(engine, run_id) == [
            "run.created",
            "run.queued",
            "attempt.claimed",
            "attempt.expired",
            "recovery.pre_start",
            "attempt.claimed",
            "attempt.expired",
            "recovery.pre_start",
            "attempt.claimed",
            "attempt.expired",
            "recovery.pre_start",
            "run.failed",
        ]
    finally:
        engine.dispose()


def test_reclaim_never_invokes_a_provider_and_writes_no_provider_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reclamation is infrastructure-only: no model call and no provider taxonomy is involved."""
    engine = prepare(tmp_path, monkeypatch)
    try:
        claim(engine)
        reclaim(engine)
        with engine.connect() as connection:
            codes = (
                connection.execute(
                    text(
                        "SELECT error_code FROM jobs UNION SELECT error_code FROM runs"
                        " UNION SELECT error_code FROM job_attempts"
                        " UNION SELECT code FROM run_events"
                    )
                )
                .scalars()
                .all()
            )
        assert all(code is None for code in codes)
    finally:
        engine.dispose()


def test_a_live_lease_is_never_reclaimed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unexpired lease is authority: reclamation must not touch it."""
    engine = prepare(tmp_path, monkeypatch)
    try:
        claimed = claim(engine)
        persistence = SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None)
        assert persistence.reclaim_next_expired_claim(now=NOW, backoff=timedelta(seconds=5)) is None
        assert job_row(engine, claimed.job_id)["status"] == JobStatus.CLAIMED.value
        assert attempt_row(engine, claimed.attempt_id)["status"] == AttemptStatus.CLAIMED.value
    finally:
        engine.dispose()


def test_post_start_loss_is_ambiguous_and_never_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash after the start boundary commits is AMBIGUOUS, and zero providers are called."""
    engine = prepare(tmp_path, monkeypatch)
    try:
        claimed = claim(engine)
        persistence = SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None)
        assert persistence.start_attempt(claimed, now=NOW) is True

        outcome = persistence.reclaim_next_expired_claim(
            now=LEASE_EXPIRED, backoff=timedelta(seconds=5)
        )
        assert outcome is not None and outcome.kind is ReclamationKind.POST_START_AMBIGUOUS

        attempt = attempt_row(engine, claimed.attempt_id)
        assert attempt["status"] == AttemptStatus.EXPIRED.value
        assert attempt["retry_disposition"] == "AMBIGUOUS"
        assert attempt["execution_started_at"] is not None

        run = run_row(engine, claimed.run_id)
        assert run["status"] == RunStatus.FAILED.value
        assert run["started_at"] == stored(NOW)  # the Run's REAL start, never fabricated
        assert run["elapsed_ms"] is not None and cast(int, run["elapsed_ms"]) >= 0
        assert run["error_code"] == EXECUTION_OUTCOME_AMBIGUOUS
        assert run["output_text"] is None

        # Never requeued: the Job stays terminal and no new claim is possible.
        assert job_row(engine, claimed.job_id)["status"] == JobStatus.FAILED.value
        assert (
            persistence.claim_next(
                worker_id="worker-2",
                provider_ids=(PROVIDER,),
                max_active=4,
                now=LEASE_EXPIRED + LEASE_DURATION,
                lease_duration=LEASE_DURATION,
            )
            is None
        )
        assert event_types(engine, claimed.run_id)[-3:] == [
            "attempt.expired",
            "recovery.ambiguous",
            "run.failed",
        ]
    finally:
        engine.dispose()


def test_registry_heartbeat_is_monotonic_and_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = prepare(tmp_path, monkeypatch)
    try:
        persistence = SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None)
        persistence.register_worker(worker_id="worker-1", now=NOW)

        assert (
            persistence.heartbeat_worker(worker_id="worker-1", now=NOW + timedelta(seconds=20))
            is WorkerLiveness.RENEWED
        )
        later = persistence.list_workers(
            now=NOW + timedelta(seconds=20), stale_after=timedelta(seconds=60)
        )
        assert later[0].state is WorkerState.HEALTHY

        # A backwards clock must not make a healthy process look older.
        assert (
            persistence.heartbeat_worker(worker_id="worker-1", now=NOW) is WorkerLiveness.REGRESSED
        )
        unchanged = persistence.list_workers(
            now=NOW + timedelta(seconds=20), stale_after=timedelta(seconds=60)
        )
        assert unchanged[0].last_heartbeat_at == later[0].last_heartbeat_at

        # Past the stale threshold the incarnation is stale -- not dead.
        stale = persistence.list_workers(
            now=NOW + timedelta(seconds=200), stale_after=timedelta(seconds=60)
        )
        assert stale[0].state is WorkerState.STALE

        assert (
            persistence.stop_worker(worker_id="worker-1", now=NOW + timedelta(seconds=20)) is True
        )
        stopped = persistence.list_workers(
            now=NOW + timedelta(seconds=200), stale_after=timedelta(seconds=60)
        )
        assert stopped[0].state is WorkerState.STOPPED
        assert (
            persistence.heartbeat_worker(worker_id="worker-1", now=NOW + timedelta(seconds=300))
            is WorkerLiveness.STOPPED
        )
        assert (
            persistence.heartbeat_worker(worker_id="ghost", now=NOW) is WorkerLiveness.UNREGISTERED
        )
        assert persistence.stop_worker(worker_id="ghost", now=NOW) is False
    finally:
        engine.dispose()


def test_registry_state_never_reclaims_a_live_leased_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registry staleness must never authorize recovery; only the Job lease can."""
    engine = prepare(tmp_path, monkeypatch)
    try:
        persistence = SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None)
        persistence.register_worker(worker_id="worker-1", now=NOW)
        claimed = claim(engine, worker_id="worker-1")

        # The registry row looks stale long before the lease expires.
        snapshots = persistence.list_workers(
            now=NOW + timedelta(seconds=600), stale_after=timedelta(seconds=60)
        )
        assert snapshots[0].state is WorkerState.STALE
        assert (
            persistence.reclaim_next_expired_claim(
                now=NOW + timedelta(seconds=600), backoff=RECLAIM_BACKOFF
            )
            is not None
        )  # the LEASE is expired by then, which is the real authority
        # Budget remained, so this is a safe requeue -- reached by lease expiry, not by the
        # registry row having gone stale.
        assert job_row(engine, claimed.job_id)["status"] == JobStatus.QUEUED.value

        # And with a live lease, a stale registry changes nothing at all.
        engine2 = migrate(tmp_path / "second.db", monkeypatch)
        try:
            SqlAlchemyJobPersistence(engine2, max_pending=1000).submit(
                owner_user_id=1,
                agent_instance_id=1,
                input_text="hello",
                limits=STAGE_B_LIMITS,
                now=NOW,
            )
            p2 = SqlAlchemyJobExecutionPersistence(engine2, sleep=lambda _: None)
            p2.register_worker(worker_id="worker-9", now=NOW)
            live = p2.claim_next(
                worker_id="worker-9",
                provider_ids=(PROVIDER,),
                max_active=4,
                now=NOW,
                lease_duration=LEASE_DURATION,
            )
            assert live is not None
            assert (
                p2.list_workers(
                    now=NOW + timedelta(seconds=600), stale_after=timedelta(seconds=60)
                )[0].state
                is WorkerState.STALE
            )
            assert (
                p2.reclaim_next_expired_claim(
                    now=NOW + timedelta(seconds=30), backoff=timedelta(seconds=5)
                )
                is None
            )
            assert job_row(engine2, live.job_id)["status"] == JobStatus.CLAIMED.value
        finally:
            engine2.dispose()
    finally:
        engine.dispose()


def job_row_of(engine: Engine, run_id: int) -> int:
    with engine.connect() as connection:
        return int(connection.scalar(text("SELECT id FROM jobs WHERE run_id=:r"), {"r": run_id}))
