"""C4 durable safe-execution-retry: the retry_wait transition, due claims, and its boundaries.

Every test runs against a database built by the real migrations and drives the real
persistence methods, because the properties under test — a claimless Job waiting on an absolute
instant, a fresh Attempt with a fresh token, one provider call per started Attempt — only exist
in committed state.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from execution_support import NOW, attempt_row, counts, event_types, job_row, migrate
from nervos_core.application.job_execution import (
    LEASE_DURATION,
    ClaimedAttempt,
    ClaimState,
    FailureOutcome,
)
from nervos_core.application.model_completion import (
    INTERNAL_EXECUTION_ERROR,
    MODEL_RATE_LIMITED,
    MODEL_UNAVAILABLE,
    safe_error_message,
)
from nervos_core.application.retry_policy import PRODUCTION_RETRY_POLICY, retry_due_at
from nervos_core.domain.jobs import AttemptStatus, JobStatus, RetryDisposition
from nervos_core.domain.runs import STAGE_B_LIMITS, ModelUsage, Run
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobExecutionPersistence,
    SqlAlchemyJobPersistence,
)
from sqlalchemy import Engine, text

PROVIDER = "anthropic"
USAGE = ModelUsage(11, 7, 18)
RATE_LIMIT_MESSAGE = safe_error_message(MODEL_RATE_LIMITED)


def prepared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, max_attempts: int = 3
) -> tuple[Engine, Run]:
    engine = migrate(tmp_path / "retry.db", monkeypatch)
    run = SqlAlchemyJobPersistence(engine, max_pending=1000).submit(
        owner_user_id=1,
        agent_instance_id=1,
        input_text="hello",
        limits=STAGE_B_LIMITS,
        now=NOW,
        max_attempts=max_attempts,
    )
    return engine, run


def execution(engine: Engine) -> SqlAlchemyJobExecutionPersistence:
    return SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None)


def claim_now(
    engine: Engine, *, now: datetime, worker_id: str = "worker-1"
) -> ClaimedAttempt | None:
    return execution(engine).claim_next(
        worker_id=worker_id,
        provider_ids=(PROVIDER,),
        max_active=4,
        now=now,
        lease_duration=LEASE_DURATION,
    )


def started(engine: Engine, *, now: datetime, worker_id: str = "worker-1") -> ClaimedAttempt:
    claimed = claim_now(engine, now=now, worker_id=worker_id)
    assert claimed is not None
    assert execution(engine).start_attempt(claimed, now=now) is True
    return claimed


def rate_limited(
    engine: Engine,
    claimed: ClaimedAttempt,
    *,
    now: datetime,
    anchor: datetime | None = None,
) -> FailureOutcome:
    return execution(engine).record_failure(
        claimed,
        error_code=MODEL_RATE_LIMITED,
        error_message=RATE_LIMIT_MESSAGE,
        retry_disposition=RetryDisposition.SAFE_TO_RETRY,
        usage=USAGE,
        elapsed_ms=9,
        anchor_at=now if anchor is None else anchor,
        retry_policy=PRODUCTION_RETRY_POLICY,
        now=now,
    )


def stored(value: datetime) -> str:
    """Render an instant exactly as the SQLite adapter persists an aware UTC timestamp."""
    return value.astimezone(UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")


def rows(engine: Engine) -> list[dict[str, object]]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text("SELECT id, attempt_number, status FROM job_attempts ORDER BY attempt_number")
            ).mappings()
        ]


# -- the retry_wait transition ----------------------------------------------------------


def test_a_rate_limited_failure_becomes_durable_retry_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, run = prepared(tmp_path, monkeypatch)
    try:
        claimed = started(engine, now=NOW)
        assert rate_limited(engine, claimed, now=NOW) is FailureOutcome.RETRY_SCHEDULED

        attempt = attempt_row(engine, claimed.attempt_id)
        assert attempt["status"] == AttemptStatus.FAILED.value
        assert attempt["retry_disposition"] == RetryDisposition.SAFE_TO_RETRY.value
        assert attempt["error_code"] == MODEL_RATE_LIMITED
        assert attempt["execution_started_at"] is not None

        job = job_row(engine, claimed.job_id)
        assert job["status"] == JobStatus.RETRY_WAIT.value
        assert job["finished_at"] is None
        assert job["error_code"] is None and job["error_message"] is None
        assert job["claimed_by"] is None and job["claim_token"] is None
        assert job["lease_expires_at"] is None and job["last_heartbeat_at"] is None
        assert job["attempt_count"] == 1

        run_row = (
            engine.connect()
            .execute(
                text("SELECT status, started_at, finished_at, error_code, elapsed_ms FROM runs")
            )
            .mappings()
            .one()
        )
        assert run_row["status"] == "running"
        assert run_row["started_at"] is not None
        assert run_row["finished_at"] is None and run_row["error_code"] is None
        assert run_row["elapsed_ms"] is None
        assert event_types(engine, run.id)[-2:] == ["attempt.failed", "retry.scheduled"]
    finally:
        engine.dispose()


def test_the_retry_event_carries_the_exact_persisted_due_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _run = prepared(tmp_path, monkeypatch)
    try:
        claimed = started(engine, now=NOW)
        assert rate_limited(engine, claimed, now=NOW) is FailureOutcome.RETRY_SCHEDULED

        with engine.connect() as connection:
            due = connection.scalar(text("SELECT available_at FROM jobs"))
            event_due = connection.scalar(
                text("SELECT available_at FROM run_events WHERE event_type='retry.scheduled'")
            )
            sequence = connection.scalar(
                text("SELECT sequence FROM run_events WHERE event_type='retry.scheduled'")
            )
            base = connection.scalar(text("SELECT max(sequence) FROM run_events"))
        # The first retry after a single started execution uses the base delay.
        assert due == stored(NOW + timedelta(seconds=1))
        assert event_due == due
        assert sequence == base
        # run.created, run.queued, attempt.claimed, attempt.started, attempt.failed,
        # retry.scheduled
        assert counts(engine)["run_events"] == 6
    finally:
        engine.dispose()


def test_the_run_is_left_running_and_its_original_start_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, run = prepared(tmp_path, monkeypatch)
    try:
        first = started(engine, now=NOW)
        with engine.connect() as connection:
            original_start = connection.scalar(text("SELECT started_at FROM runs"))
        assert rate_limited(engine, first, now=NOW) is FailureOutcome.RETRY_SCHEDULED
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT started_at FROM runs")) == original_start

        later = NOW + timedelta(seconds=30)
        second = started(engine, now=later)
        assert second.attempt_number == 2
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT started_at FROM runs")) == original_start
            assert connection.scalar(text("SELECT status FROM runs")) == "running"
        # One start transition for the Run, one start event per Attempt.
        assert [row["status"] for row in rows(engine)] == ["failed", "running"]
        assert event_types(engine, run.id).count("attempt.started") == 2
    finally:
        engine.dispose()


# -- due claim --------------------------------------------------------------------------


def test_retry_wait_is_not_claimable_before_its_due_instant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _run = prepared(tmp_path, monkeypatch)
    try:
        claimed = started(engine, now=NOW)
        assert rate_limited(engine, claimed, now=NOW) is FailureOutcome.RETRY_SCHEDULED

        assert claim_now(engine, now=NOW) is None
        assert claim_now(engine, now=NOW + timedelta(milliseconds=999)) is None
        assert claim_now(engine, now=NOW + timedelta(seconds=1)) is not None
        assert counts(engine)["job_attempts"] == 2
    finally:
        engine.dispose()


def test_a_due_retry_creates_a_fresh_attempt_and_a_fresh_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _run = prepared(tmp_path, monkeypatch)
    try:
        first = started(engine, now=NOW)
        assert rate_limited(engine, first, now=NOW) is FailureOutcome.RETRY_SCHEDULED

        second = claim_now(engine, now=NOW + timedelta(seconds=1))
        assert second is not None
        assert second.attempt_id != first.attempt_id
        assert second.attempt_number == 2
        assert second.claim_token != first.claim_token
        assert len(second.claim_token) == 32
        job = job_row(engine, first.job_id)
        assert job["status"] == JobStatus.CLAIMED.value
        assert job["attempt_count"] == 2
        assert event_types(engine, first.run_id)[-1] == "attempt.claimed"
    finally:
        engine.dispose()


def test_the_backoff_ordinal_ignores_pre_start_claim_losses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claim that died before the start boundary consumed budget but never called a provider."""
    engine, _run = prepared(tmp_path, monkeypatch, max_attempts=4)
    try:
        # A pre-start loss: claimed, never started, then reclaimed as expired by C3's path.
        lost = claim_now(engine, now=NOW)
        assert lost is not None
        with engine.connect() as connection:
            connection.execute(
                text(
                    "UPDATE job_attempts SET status='expired', finished_at=:now,"
                    " retry_disposition='SAFE_TO_RETRY' WHERE id=:a"
                ),
                {"now": "2026-09-14 00:00:00.000000", "a": lost.attempt_id},
            )
            connection.execute(
                text("UPDATE jobs SET status='queued' WHERE id=:j"), {"j": lost.job_id}
            )
            connection.commit()

        first = started(engine, now=NOW + timedelta(minutes=5))
        assert rate_limited(engine, first, now=NOW + timedelta(minutes=5)) is (
            FailureOutcome.RETRY_SCHEDULED
        )
        job = job_row(engine, first.job_id)
        # Budget counted the lost claim, so this is claim 2; the delay is still the first one,
        # because only one execution has actually failed safely.
        assert job["attempt_count"] == 2
        assert job["available_at"] == stored(NOW + timedelta(minutes=5) + timedelta(seconds=1))
    finally:
        engine.dispose()


def test_the_second_safe_failure_uses_the_second_delay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _run = prepared(tmp_path, monkeypatch, max_attempts=4)
    try:
        first = started(engine, now=NOW)
        assert rate_limited(engine, first, now=NOW) is FailureOutcome.RETRY_SCHEDULED
        second_at = NOW + timedelta(seconds=1)
        second = started(engine, now=second_at)
        anchor = second_at + timedelta(seconds=5)
        assert rate_limited(engine, second, now=anchor) is FailureOutcome.RETRY_SCHEDULED
        assert job_row(engine, first.job_id)["available_at"] == stored(
            anchor + timedelta(seconds=2)
        )
    finally:
        engine.dispose()


def test_the_shared_claim_budget_bounds_the_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`max_attempts` remains a total committed-claim budget, not a provider-call budget."""
    engine, run = prepared(tmp_path, monkeypatch, max_attempts=3)
    try:
        first = started(engine, now=NOW)
        assert rate_limited(engine, first, now=NOW) is FailureOutcome.RETRY_SCHEDULED
        second = started(engine, now=NOW + timedelta(seconds=1))
        assert rate_limited(engine, second, now=NOW + timedelta(seconds=1)) is (
            FailureOutcome.RETRY_SCHEDULED
        )
        third = started(engine, now=NOW + timedelta(seconds=3))
        # The third claim was the last unit of budget, so this failure cannot be rescheduled.
        assert rate_limited(engine, third, now=NOW + timedelta(seconds=3)) is (
            FailureOutcome.TERMINAL_FAILED
        )

        job = job_row(engine, first.job_id)
        assert job["status"] == JobStatus.FAILED.value
        assert job["attempt_count"] == 3
        assert job["error_code"] == MODEL_RATE_LIMITED
        assert job["claimed_by"] is None and job["finished_at"] is not None
        with engine.connect() as connection:
            run_row = (
                connection.execute(text("SELECT status, error_code, elapsed_ms FROM runs"))
                .mappings()
                .one()
            )
        assert run_row["status"] == "failed"
        assert run_row["error_code"] == MODEL_RATE_LIMITED
        assert run_row["elapsed_ms"] is not None

        events = event_types(engine, run.id)
        assert events.count("retry.scheduled") == 2
        assert events.count("run.failed") == 1
        assert events[-1] == "run.failed"
        assert len(rows(engine)) == 3
        assert claim_now(engine, now=NOW + timedelta(hours=1)) is None
    finally:
        engine.dispose()


def test_a_nonretryable_failure_never_enters_retry_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, run = prepared(tmp_path, monkeypatch)
    try:
        claimed = started(engine, now=NOW)
        settled = execution(engine).record_failure(
            claimed,
            error_code=MODEL_UNAVAILABLE,
            error_message=safe_error_message(MODEL_UNAVAILABLE),
            retry_disposition=RetryDisposition.AMBIGUOUS,
            usage=USAGE,
            elapsed_ms=3,
            anchor_at=NOW,
            retry_policy=PRODUCTION_RETRY_POLICY,
            now=NOW,
        )
        assert settled is FailureOutcome.TERMINAL_FAILED
        assert job_row(engine, claimed.job_id)["status"] == JobStatus.FAILED.value
        assert "retry.scheduled" not in event_types(engine, run.id)
        assert claim_now(engine, now=NOW + timedelta(hours=1)) is None
    finally:
        engine.dispose()


# -- fencing and races ------------------------------------------------------------------


def test_a_stale_token_cannot_schedule_a_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _run = prepared(tmp_path, monkeypatch)
    try:
        claimed = started(engine, now=NOW)
        before = job_row(engine, claimed.job_id)
        settled = execution(engine).record_failure(
            claimed,
            error_code=MODEL_RATE_LIMITED,
            error_message=RATE_LIMIT_MESSAGE,
            retry_disposition=RetryDisposition.SAFE_TO_RETRY,
            usage=USAGE,
            elapsed_ms=3,
            anchor_at=NOW,
            retry_policy=PRODUCTION_RETRY_POLICY,
            now=NOW + timedelta(minutes=5),
        )
        # The lease had expired, so the write affects zero rows and changes nothing.
        assert settled is FailureOutcome.UNRESOLVED
        assert job_row(engine, claimed.job_id) == before
        assert counts(engine)["run_events"] == 4
    finally:
        engine.dispose()


def test_an_expired_claim_cannot_schedule_a_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _run = prepared(tmp_path, monkeypatch)
    try:
        claimed = started(engine, now=NOW)
        settled = execution(engine).record_failure(
            claimed,
            error_code=MODEL_RATE_LIMITED,
            error_message=RATE_LIMIT_MESSAGE,
            retry_disposition=RetryDisposition.SAFE_TO_RETRY,
            usage=USAGE,
            elapsed_ms=3,
            anchor_at=NOW,
            retry_policy=PRODUCTION_RETRY_POLICY,
            now=NOW + LEASE_DURATION + timedelta(seconds=1),
        )
        assert settled is FailureOutcome.UNRESOLVED
        assert "retry.scheduled" not in event_types(engine, claimed.run_id)
    finally:
        engine.dispose()


def test_the_same_attempt_cannot_be_settled_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _run = prepared(tmp_path, monkeypatch)
    try:
        claimed = started(engine, now=NOW)
        assert rate_limited(engine, claimed, now=NOW) is FailureOutcome.RETRY_SCHEDULED
        before = counts(engine)
        # A second settlement of the same Attempt is not a transition: it is already terminal.
        assert rate_limited(engine, claimed, now=NOW) is FailureOutcome.RETRY_SCHEDULED
        assert counts(engine) == before
    finally:
        engine.dispose()


def test_two_workers_racing_one_due_retry_produce_exactly_one_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _run = prepared(tmp_path, monkeypatch)
    try:
        first = started(engine, now=NOW)
        assert rate_limited(engine, first, now=NOW) is FailureOutcome.RETRY_SCHEDULED
        due = NOW + timedelta(seconds=1)

        winner = execution(engine).claim_next(
            worker_id="worker-a",
            provider_ids=(PROVIDER,),
            max_active=4,
            now=due,
            lease_duration=LEASE_DURATION,
        )
        loser = execution(engine).claim_next(
            worker_id="worker-b",
            provider_ids=(PROVIDER,),
            max_active=4,
            now=due,
            lease_duration=LEASE_DURATION,
        )
        assert winner is not None and loser is None
        assert len(rows(engine)) == 2
        assert event_types(engine, first.run_id).count("attempt.claimed") == 2
        assert job_row(engine, first.job_id)["claimed_by"] == "worker-a"
    finally:
        engine.dispose()


def test_an_incompatible_worker_leaves_a_retry_wait_job_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _run = prepared(tmp_path, monkeypatch)
    try:
        claimed = started(engine, now=NOW)
        assert rate_limited(engine, claimed, now=NOW) is FailureOutcome.RETRY_SCHEDULED
        due = NOW + timedelta(seconds=1)

        assert (
            execution(engine).claim_next(
                worker_id="worker-openai",
                provider_ids=("openai",),
                max_active=4,
                now=due,
                lease_duration=LEASE_DURATION,
            )
            is None
        )
        assert job_row(engine, claimed.job_id)["status"] == JobStatus.RETRY_WAIT.value
        assert claim_now(engine, now=due) is not None
    finally:
        engine.dispose()


def test_retry_wait_does_not_consume_active_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _run = prepared(tmp_path, monkeypatch)
    try:
        claimed = started(engine, now=NOW)
        assert rate_limited(engine, claimed, now=NOW) is FailureOutcome.RETRY_SCHEDULED
        # A cap of one must not be consumed by a Job that holds no live lease.
        blocked = execution(engine).claim_next(
            worker_id="worker-1",
            provider_ids=(PROVIDER,),
            max_active=0,
            now=NOW + timedelta(seconds=1),
            lease_duration=LEASE_DURATION,
        )
        assert blocked is None
        due = claim_now(engine, now=NOW + timedelta(seconds=1))
        assert due is not None
        # Once claimed it does occupy active capacity like any other execution.
        assert (
            execution(engine).claim_next(
                worker_id="worker-2",
                provider_ids=(PROVIDER,),
                max_active=1,
                now=NOW + timedelta(seconds=2),
                lease_duration=LEASE_DURATION,
            )
            is None
        )
    finally:
        engine.dispose()


def test_retry_wait_keeps_occupying_pending_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retried Job is one existing pending obligation; it must not leak or duplicate a slot."""
    from nervos_core.application.errors import QueueCapacityExceeded

    engine, _run = prepared(tmp_path, monkeypatch)
    try:
        claimed = started(engine, now=NOW)
        assert rate_limited(engine, claimed, now=NOW) is FailureOutcome.RETRY_SCHEDULED
        # A second submission with the budget already spent by the pending retry is refused.
        with pytest.raises(QueueCapacityExceeded):
            SqlAlchemyJobPersistence(engine, max_pending=1).submit(
                owner_user_id=1,
                agent_instance_id=1,
                input_text="second",
                limits=STAGE_B_LIMITS,
                now=NOW + timedelta(seconds=1),
            )
    finally:
        engine.dispose()


def test_an_uncommitted_failure_is_proven_unsettled_before_any_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _run = prepared(tmp_path, monkeypatch)
    try:
        claimed = started(engine, now=NOW)
        state = execution(engine).inspect_failure(
            claimed,
            error_code=MODEL_RATE_LIMITED,
            retry_disposition=RetryDisposition.SAFE_TO_RETRY,
            anchor_at=NOW,
            retry_policy=PRODUCTION_RETRY_POLICY,
            now=NOW,
        )
        # Nothing settled yet and the claim is still live, so only the durable write may be
        # replayed — never the provider call that already happened.
        assert state is FailureOutcome.UNSETTLED
        assert execution(engine).inspect_claim(claimed, now=NOW) is ClaimState.ACTIVE
    finally:
        engine.dispose()


def test_a_settled_retry_is_reconcilable_from_durable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _run = prepared(tmp_path, monkeypatch)
    try:
        claimed = started(engine, now=NOW)
        assert rate_limited(engine, claimed, now=NOW) is FailureOutcome.RETRY_SCHEDULED
        reconciled = execution(engine).inspect_failure(
            claimed,
            error_code=MODEL_RATE_LIMITED,
            retry_disposition=RetryDisposition.SAFE_TO_RETRY,
            anchor_at=NOW,
            retry_policy=PRODUCTION_RETRY_POLICY,
            now=NOW,
        )
        assert reconciled is FailureOutcome.RETRY_SCHEDULED
    finally:
        engine.dispose()


def test_a_different_anchor_does_not_adopt_another_operators_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconciliation compares the deadline, so an unrelated schedule is never mistaken for ours."""
    engine, _run = prepared(tmp_path, monkeypatch)
    try:
        claimed = started(engine, now=NOW)
        assert rate_limited(engine, claimed, now=NOW) is FailureOutcome.RETRY_SCHEDULED
        reconciled = execution(engine).inspect_failure(
            claimed,
            error_code=MODEL_RATE_LIMITED,
            retry_disposition=RetryDisposition.SAFE_TO_RETRY,
            anchor_at=NOW + timedelta(hours=1),
            retry_policy=PRODUCTION_RETRY_POLICY,
            now=NOW,
        )
        assert reconciled is FailureOutcome.UNRESOLVED
    finally:
        engine.dispose()


def test_a_replayed_transition_keeps_the_deadline_it_already_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database-only replay must not move the deadline the transition already committed.

    This is the property that makes an uncertain COMMIT reconcilable: the same logical
    operation, anchored once, recomputes exactly one expected due time no matter how many times
    the write is replayed, and a replay writes no second event.
    """
    engine, _run = prepared(tmp_path, monkeypatch)
    try:
        claimed = started(engine, now=NOW)
        expected = retry_due_at(PRODUCTION_RETRY_POLICY, anchor_at=NOW, ordinal=1)
        assert rate_limited(engine, claimed, now=NOW) is FailureOutcome.RETRY_SCHEDULED
        assert job_row(engine, claimed.job_id)["available_at"] == stored(expected)
        before = counts(engine)

        # A replay reaches the same conclusion from the same anchor, even though the
        # transaction's own clock has moved on, and adds nothing.
        replayed = rate_limited(engine, claimed, now=NOW + timedelta(seconds=30), anchor=NOW)
        assert replayed is FailureOutcome.RETRY_SCHEDULED
        assert job_row(engine, claimed.job_id)["available_at"] == stored(expected)
        assert counts(engine) == before
    finally:
        engine.dispose()


def test_an_internal_failure_never_schedules_a_retry_even_with_budget_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, run = prepared(tmp_path, monkeypatch)
    try:
        claimed = started(engine, now=NOW)
        settled = execution(engine).record_failure(
            claimed,
            error_code=INTERNAL_EXECUTION_ERROR,
            error_message=safe_error_message(INTERNAL_EXECUTION_ERROR),
            retry_disposition=RetryDisposition.AMBIGUOUS,
            usage=USAGE,
            elapsed_ms=3,
            anchor_at=NOW,
            retry_policy=PRODUCTION_RETRY_POLICY,
            now=NOW,
        )
        assert settled is FailureOutcome.TERMINAL_FAILED
        assert event_types(engine, run.id)[-2:] == ["attempt.failed", "run.failed"]
    finally:
        engine.dispose()


def test_the_due_work_query_uses_the_existing_status_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C4 adds `retry_wait` to a query that must stay index-driven, not become a scan.

    The candidate query is the queue's hot path, and C4 widens it from one status to two. This
    asserts SQLite still resolves both branches through the existing C3 index rather than
    scanning `jobs`, so activating retries does not quietly change the queue's cost profile.
    """
    engine, _run = prepared(tmp_path, monkeypatch)
    try:
        with engine.connect() as connection:
            plan = [
                str(row[3])
                for row in connection.exec_driver_sql(
                    "EXPLAIN QUERY PLAN "
                    "SELECT jobs.id FROM jobs JOIN runs ON runs.id = jobs.run_id "
                    "WHERE ((jobs.status='queued' AND runs.status='created') "
                    "OR (jobs.status='retry_wait' AND runs.status='running')) "
                    "AND jobs.available_at <= '2026-09-14 00:01:00' "
                    "AND jobs.attempt_count < jobs.max_attempts "
                    "AND jobs.model_provider IN ('anthropic') "
                    "ORDER BY jobs.available_at ASC, jobs.id ASC LIMIT 1"
                )
            ]
        assert any("ix_jobs_status_available_at_id" in step for step in plan), plan
        assert not any(step.startswith("SCAN ") for step in plan), plan
    finally:
        engine.dispose()


def test_the_worker_recovery_exhausted_shape_is_never_reused_for_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retry exhaustion surfaces the real provider error, not the C3 infrastructure code."""
    engine, _run = prepared(tmp_path, monkeypatch, max_attempts=1)
    try:
        claimed = started(engine, now=NOW)
        assert rate_limited(engine, claimed, now=NOW) is FailureOutcome.TERMINAL_FAILED
        with engine.connect() as connection:
            row = (
                connection.execute(text("SELECT status, started_at, error_code FROM runs"))
                .mappings()
                .one()
            )
        assert row["error_code"] == MODEL_RATE_LIMITED
        assert row["started_at"] is not None
    finally:
        engine.dispose()


def test_a_second_attempt_can_succeed_and_terminalize_the_run_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, run = prepared(tmp_path, monkeypatch)
    try:
        first = started(engine, now=NOW)
        assert rate_limited(engine, first, now=NOW) is FailureOutcome.RETRY_SCHEDULED
        second = started(engine, now=NOW + timedelta(seconds=1))
        assert execution(engine).succeed(
            second,
            output_text="recovered answer",
            finish_reason="stop",
            usage=USAGE,
            elapsed_ms=12,
            now=NOW + timedelta(seconds=2),
        )

        with engine.connect() as connection:
            run_row = (
                connection.execute(
                    text(
                        "SELECT status, started_at, finished_at, output_text, elapsed_ms,"
                        " error_code FROM runs"
                    )
                )
                .mappings()
                .one()
            )
        assert run_row["status"] == "succeeded"
        assert run_row["output_text"] == "recovered answer"
        assert run_row["error_code"] is None
        # The Run's start is the first execution's start, not the retry's.
        assert run_row["started_at"] == stored(NOW)
        # The Run's elapsed time stays the terminal Attempt's execution duration.
        assert run_row["elapsed_ms"] == 12
        events = event_types(engine, run.id)
        assert events[-1] == "run.succeeded"
        assert events.count("run.succeeded") == 1
        assert events.count("attempt.started") == 2
        assert events.count("run.failed") == 0
        assert [row["status"] for row in rows(engine)] == ["failed", "succeeded"]
    finally:
        engine.dispose()
