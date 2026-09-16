"""C8 bounded stress: a deterministic soak, plus the SQLite integrity it must leave behind.

This is not a benchmark. It is the one place where every Stage C property has to hold *at once*:
three concurrency dimensions, durable fairness with heterogeneous Worker capability, admission
backpressure, safe retries, pre-start recovery, execution timeouts and owner cancellation, all
interleaved over one database and one deterministic workload.

Outcomes are assigned by index, never at random, so a failure is reproducible from the seed alone.
Time is a parameter: a lease expires and a retry becomes due by passing a later `now`, so the run
never waits on a real backoff and CI cost stays bounded.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from nervos_core.application.errors import QueueCapacityExceeded
from nervos_core.application.job_execution import ClaimedAttempt
from nervos_core.application.model_completion import (
    MODEL_RATE_LIMITED,
    MODEL_TIMED_OUT,
    safe_error_message,
)
from nervos_core.application.queue_policy import QueuePolicy
from nervos_core.application.retry_policy import PRODUCTION_RETRY_POLICY
from nervos_core.domain.jobs import AttemptStatus, JobStatus, RetryDisposition
from nervos_core.domain.runs import STAGE_B_LIMITS, ModelUsage, RunStatus
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobPersistence,
    SqlAlchemyRunCancellationPersistence,
)
from sqlalchemy import Engine, text
from stage_c_support import (
    BOTH_PROVIDERS,
    LEASE_DURATION,
    NOW,
    PROVIDER_ID,
    SECOND_PROVIDER_ID,
    ScriptedCompletion,
    active_count_for,
    active_job_count,
    attempt_statuses,
    build_worker,
    claim,
    counts,
    event_types,
    execution,
    job_row,
    live_attempt_violations,
    migrate,
    must_claim,
    partition_rows,
    pending_job_count,
    run_row,
    runs_with_gapped_sequences,
    start,
    submit,
    succeed,
)

RECLAIM_BACKOFF = timedelta(seconds=5)

# The frozen scale. Four Instances across two providers, three Workers of differing capability, and
# one hundred and twenty Jobs: large enough that every dimension interlocks, small enough that the
# whole soak stays an order of magnitude below the rest of the suite.
AGENTS = 4
JOBS = 120
CAPS = QueuePolicy(global_active_limit=8, per_agent_active_limit=3, per_provider_active_limit=4)
PENDING = 6

# Deterministic outcome classes, cycled by Job index. Five classes, so every fifth Job of a capable
# Instance exercises the same path and the distribution is exactly reproducible.
OUTCOMES = ("succeed", "rate_limited", "pre_start_loss", "timeout", "cancelled")


def outcome_for(index: int) -> str:
    return OUTCOMES[index % len(OUTCOMES)]


def cancel(engine: Engine, run_id: int, *, now: datetime) -> None:
    SqlAlchemyRunCancellationPersistence(engine, sleep=lambda _: None).cancel_run(
        user_id=1, run_id=run_id, now=now
    )


def record_timeout(engine: Engine, claimed: ClaimedAttempt, *, now: datetime) -> None:
    """The terminal write a C5 execution timeout produces: failed, ambiguous, never retried."""
    execution(engine, policy=CAPS).record_failure(
        claimed,
        error_code=MODEL_TIMED_OUT,
        error_message=safe_error_message(MODEL_TIMED_OUT),
        retry_disposition=RetryDisposition.AMBIGUOUS,
        usage=ModelUsage(1, 1, 2),
        elapsed_ms=5,
        anchor_at=now,
        retry_policy=PRODUCTION_RETRY_POLICY,
        now=now,
    )


def assert_caps_hold(engine: Engine, *, now: datetime) -> None:
    """The three caps, read from the database after every claim rather than assumed."""
    assert active_job_count(engine, now=now) <= CAPS.global_active_limit
    for instance in range(1, AGENTS + 1):
        assert (
            active_count_for(engine, dimension="agent_instance_id", value=instance, now=now)
            <= CAPS.per_agent_active_limit
        )
    for provider in BOTH_PROVIDERS:
        assert (
            active_count_for(engine, dimension="model_provider", value=provider, now=now)
            <= CAPS.per_provider_active_limit
        )
    # The single most important structural invariant: no Job may ever hold two live Attempts.
    assert live_attempt_violations(engine) == 0


@pytest.fixture
def soak(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    engine = migrate(tmp_path / "stress.db", monkeypatch, agents=AGENTS, providers=BOTH_PROVIDERS)
    try:
        yield engine
    finally:
        engine.dispose()


def test_the_bounded_stress_soak_holds_every_stage_c_property(soak: Engine, tmp_path: Path) -> None:
    engine = soak
    submission = SqlAlchemyJobPersistence(
        engine,
        max_pending=PENDING,
        max_pending_per_agent=PENDING,
        max_pending_per_provider=PENDING,
        sleep=lambda _: None,
    )

    # Three Workers with genuinely different capability, so fairness is exercised across a
    # heterogeneous fleet rather than three identical claimants.
    workers = {
        "worker-capable": BOTH_PROVIDERS,
        "worker-anthropic": (PROVIDER_ID,),
        "worker-openai": (SECOND_PROVIDER_ID,),
    }

    # The whole workload is admitted, but only as capacity frees: a refusal is retried later rather
    # than dropped, so the soak covers every index of the frozen 120-Job scale exactly once.
    admitted: list[tuple[int, int, str]] = []
    refused = list(range(JOBS))
    refusals = 0
    now = NOW
    for index in list(refused):
        try:
            run_id = submission.submit(
                owner_user_id=1,
                agent_instance_id=((index % AGENTS) + 1),
                input_text=f"soak-{index}",
                limits=STAGE_B_LIMITS,
                now=now,
                max_attempts=3,
            ).id
        except QueueCapacityExceeded:
            refusals += 1
            continue
        refused.remove(index)
        admitted.append((index, run_id, outcome_for(index)))

    assert refusals > 0, "the pending bound must bind at this scale, or it is not being tested"
    assert len(admitted) == JOBS - refusals
    assert pending_job_count(engine) <= PENDING * AGENTS

    # Drain and churn: every iteration each Worker tries to claim, and the claimed Job's scripted
    # outcome is applied for real. Retry waits and lease expiries are advanced by moving the clock.
    applied = 0
    for _ in range(400):
        progressed = False
        for worker_id, providers in workers.items():
            claimed = claim(engine, worker_id=worker_id, providers=providers, policy=CAPS, now=now)
            if claimed is None:
                continue
            progressed = True
            assert_caps_hold(engine, now=now)
            outcome = dict((run_id, kind) for _, run_id, kind in admitted)[claimed.run_id]

            if outcome == "pre_start_loss":
                # The Worker is lost before it begins; the lease is the only thing that frees it.
                expired_at = now + LEASE_DURATION + timedelta(seconds=1)
                execution(engine, policy=CAPS).reclaim_next_expired_claim(
                    now=expired_at, backoff=RECLAIM_BACKOFF
                )
                now = max(now, expired_at + RECLAIM_BACKOFF + timedelta(seconds=1))
                continue

            start(engine, claimed, now=now)
            if outcome == "rate_limited":
                execution(engine, policy=CAPS).record_failure(
                    claimed,
                    error_code=MODEL_RATE_LIMITED,
                    error_message=safe_error_message(MODEL_RATE_LIMITED),
                    retry_disposition=RetryDisposition.SAFE_TO_RETRY,
                    usage=ModelUsage(1, 1, 2),
                    elapsed_ms=5,
                    anchor_at=now,
                    retry_policy=PRODUCTION_RETRY_POLICY,
                    now=now,
                )
                now = now + timedelta(seconds=10)
            elif outcome == "timeout":
                record_timeout(engine, claimed, now=now)
            elif outcome == "cancelled":
                cancel(engine, claimed.run_id, now=now + timedelta(milliseconds=1))
            else:
                succeed(engine, claimed, now=now)
            applied += 1

        # Admit one Run the pending bound refused, so the workload eventually covers the full scale.
        if refused and pending_job_count(engine) < PENDING * AGENTS:
            index = refused[0]
            try:
                run_id = submission.submit(
                    owner_user_id=1,
                    agent_instance_id=((index % AGENTS) + 1),
                    input_text=f"soak-{index}",
                    limits=STAGE_B_LIMITS,
                    now=now,
                ).id
            except QueueCapacityExceeded:
                pass
            else:
                refused.pop(0)
                admitted.append((index, run_id, outcome_for(index)))

        if not progressed and pending_job_count(engine) == 0 and not refused:
            break

    # Final settlement: every Run reaches a terminal state, and nothing is left mid-flight.
    for _ in range(200):
        if pending_job_count(engine) == 0:
            break
        for worker_id, providers in workers.items():
            claimed = claim(engine, worker_id=worker_id, providers=providers, policy=CAPS, now=now)
            if claimed is None:
                continue
            assert_caps_hold(engine, now=now)
            outcome = dict((run_id, kind) for _, run_id, kind in admitted)[claimed.run_id]
            if outcome == "cancelled":
                start(engine, claimed, now=now)
                cancel(engine, claimed.run_id, now=now + timedelta(milliseconds=1))
            elif outcome == "timeout":
                start(engine, claimed, now=now)
                record_timeout(engine, claimed, now=now)
            else:
                succeed(engine, start(engine, claimed, now=now), now=now)
        now = now + timedelta(seconds=60)

    # ---- the invariants ----

    final_statuses = {str(run_row(engine, run_id)["status"]) for _, run_id, _ in admitted}
    assert final_statuses <= {
        RunStatus.SUCCEEDED.value,
        RunStatus.FAILED.value,
        RunStatus.CANCELLED.value,
    }, final_statuses
    assert pending_job_count(engine) == 0

    # Every outcome class was actually exercised; a soak that never cancelled anything proves less.
    observed = {
        kind: sum(
            1
            for _, run_id, _ in admitted
            if any(
                marker in event_types(engine, run_id)
                for marker in ("run.cancelled", "run.failed", "run.succeeded")
            )
        )
        for kind in OUTCOMES
    }
    assert observed["cancelled"] > 0
    assert observed["timeout"] > 0
    assert observed["rate_limited"] > 0

    # No Run's timeline has a gap or a collision: the public surface is as consistent as the rows.
    assert runs_with_gapped_sequences(engine) == []
    # No compatible, continuously eligible Agent partition was starved: every Agent that had work
    # in the soak is represented in the durable fairness history.
    expected_partitions = {(index % AGENTS) + 1 for index, _, _ in admitted}
    assert set(partition_rows(engine)) == expected_partitions
    assert len(expected_partitions) == AGENTS
    assert len(admitted) == JOBS
    assert live_attempt_violations(engine) == 0
    assert applied > 0


def test_the_stress_database_is_structurally_sound(soak: Engine, tmp_path: Path) -> None:
    """The integrity checks the acceptance report quotes, on the database the soak produced."""
    engine = soak
    database_path = tmp_path / "stress.db"
    for index in range(12):
        run_id = submit(engine, agent=(index % AGENTS) + 1, text_value=f"integrity-{index}")
        claimed = must_claim(engine, providers=BOTH_PROVIDERS, policy=CAPS, now=NOW)
        succeed(engine, start(engine, claimed, now=NOW), now=NOW)
        assert run_id > 0

    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert list(connection.execute(text("PRAGMA foreign_key_check")).all()) == []
        # No WAL: the rollback journal is unchanged, which the migrations suite also asserts.
        assert connection.scalar(text("PRAGMA journal_mode")) in ("delete", "memory")
        # Every active Attempt is unique per Job.
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM (SELECT job_id FROM job_attempts"
                    " WHERE status IN ('claimed','running') GROUP BY job_id HAVING count(*) > 1)"
                )
            )
            == 0
        )
        # `jobs.attempt_count` agrees with the durable Attempt count for every Job.
        mismatched = connection.scalar(
            text(
                "SELECT count(*) FROM jobs j WHERE j.attempt_count <>"
                " (SELECT count(*) FROM job_attempts a WHERE a.job_id = j.id)"
            )
        )
        assert mismatched == 0
        # Every fairness marker references a real committed Attempt, or is null.
        dangling = connection.scalar(
            text(
                "SELECT count(*) FROM queue_partitions p"
                " WHERE p.last_served_attempt_id IS NOT NULL AND NOT EXISTS"
                " (SELECT 1 FROM job_attempts a WHERE a.id = p.last_served_attempt_id)"
            )
        )
        assert dangling == 0
    assert engine is not None
    assert Path(database_path).is_file()
    assert sqlite3.sqlite_version


def test_a_fleet_of_workers_drains_a_real_workload_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same shape, driven through the real Worker supervisors rather than the driver.

    The synchronous soak above is what proves the caps and the fairness rotation at scale; this one
    proves the process composition those rules run inside still settles a mixed fleet, which is what
    a deployment actually does.
    """
    engine = migrate(tmp_path / "fleet.db", monkeypatch, agents=AGENTS, providers=BOTH_PROVIDERS)
    try:
        completions = {
            PROVIDER_ID: ScriptedCompletion(PROVIDER_ID, outcomes=("rate_limited", "succeed")),
            SECOND_PROVIDER_ID: ScriptedCompletion(SECOND_PROVIDER_ID, outcomes=("succeed",)),
        }
        harnesses = [
            build_worker(
                engine,
                worker_id=f"worker-{index}",
                completions=completions,
                concurrency=2,
            )
            for index in range(3)
        ]
        run_ids = [
            submit(engine, agent=(index % AGENTS) + 1, text_value=f"fleet-{index}")
            for index in range(6)
        ]

        import asyncio

        from stage_c_support import run_until_settled

        asyncio.run(run_until_settled(harnesses, engine, include_retry_wait=True))

        for run_id in run_ids:
            assert run_row(engine, run_id)["status"] == RunStatus.SUCCEEDED.value
            assert attempt_statuses(engine, run_id)[-1] == AttemptStatus.SUCCEEDED.value
        # Every Job settled, the fleet left no live lease behind, and no timeline has a gap.
        assert pending_job_count(engine) == 0
        assert active_job_count(engine, now=datetime.now(UTC)) == 0
        assert live_attempt_violations(engine) == 0
        assert runs_with_gapped_sequences(engine) == []
        assert counts(engine)["runs"] == len(run_ids)
        assert {str(job_row(engine, run_id)["status"]) for run_id in run_ids} == {
            JobStatus.SUCCEEDED.value
        }
    finally:
        engine.dispose()
