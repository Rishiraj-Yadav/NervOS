"""C5 owner cancellation: the durable transition, its shapes, and its fences.

Every test runs against a database built by the real migrations and drives the real persistence
methods. That matters here because the properties under test — a cancelled Job that no Worker
can ever claim again, a revoked token that matches zero rows, an idempotent repeat that appends
nothing — only exist in committed state.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from execution_support import (
    NOW,
    attempt_row,
    counts,
    event_types,
    job_id_of,
    job_row,
    migrate,
    run_row,
)
from nervos_core.application.job_execution import (
    LEASE_DURATION,
    CancellationOutcome,
    ClaimedAttempt,
    ClaimState,
)
from nervos_core.application.model_completion import (
    EXECUTION_CANCELLED,
    MODEL_RATE_LIMITED,
    MODEL_UNAVAILABLE,
    safe_error_message,
)
from nervos_core.application.retry_policy import PRODUCTION_RETRY_POLICY
from nervos_core.domain.jobs import AttemptStatus, JobStatus, RetryDisposition
from nervos_core.domain.runs import STAGE_B_LIMITS, ModelUsage, Run, RunStatus
from nervos_core.infrastructure.database import create_sqlite_engine
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobExecutionPersistence,
    SqlAlchemyJobPersistence,
    SqlAlchemyRunCancellationPersistence,
)
from sqlalchemy import Engine, text

PROVIDER = "anthropic"
USAGE = ModelUsage(11, 7, 18)
CANCELLED_MESSAGE = safe_error_message(EXECUTION_CANCELLED)


def submitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, max_attempts: int = 3
) -> tuple[Engine, Run]:
    engine = migrate(tmp_path / "cancel.db", monkeypatch)
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


def cancelling(engine: Engine) -> SqlAlchemyRunCancellationPersistence:
    return SqlAlchemyRunCancellationPersistence(engine, sleep=lambda _: None)


def claim_now(engine: Engine, *, now: datetime, worker_id: str = "worker-1"):
    return execution(engine).claim_next(
        worker_id=worker_id,
        provider_ids=(PROVIDER,),
        max_active=4,
        now=now,
        lease_duration=LEASE_DURATION,
    )


def started(engine: Engine, *, now: datetime, worker_id: str = "worker-1"):
    claimed = claim_now(engine, now=now, worker_id=worker_id)
    assert claimed is not None
    assert execution(engine).start_attempt(claimed, now=now) is True
    return claimed


def stored(value: object) -> object:
    """Compare one instant in a single representation.

    Raw `text()` reads hand back SQLite's own string form, while the values written by the
    domain are aware datetimes; the microsecond suffix is an artifact of the column type, not a
    difference in the instant.
    """
    if isinstance(value, datetime):
        # The column stores naive UTC, so an aware instant has to shed its offset before it can
        # be compared against what SQLite hands back.
        naive = value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value
        return naive.isoformat(sep=" ")
    if isinstance(value, str) and "." in value:
        return value.split(".", 1)[0]
    return value


# ---------------------------------------------------------------------------------------
# Queued: cancellation before anything was claimed
# ---------------------------------------------------------------------------------------


def test_a_queued_run_is_cancelled_before_it_ever_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, run = submitted(tmp_path, monkeypatch)

    outcome = cancelling(engine).cancel_run(
        user_id=1, run_id=run.id, now=NOW + timedelta(seconds=3)
    )

    assert outcome is CancellationOutcome.CANCELLED
    assert run_row(engine, run.id)["status"] == RunStatus.CANCELLED.value
    # The pre-start shape invents no start boundary and no duration.
    assert run_row(engine, run.id)["started_at"] is None
    assert run_row(engine, run.id)["elapsed_ms"] is None
    # Cancellation is not a provider failure, so the public Run carries no error at all.
    assert run_row(engine, run.id)["error_code"] is None
    assert run_row(engine, run.id)["error_message"] is None
    assert stored(run_row(engine, run.id)["finished_at"]) == "2026-09-14 00:00:03"

    job = job_row(engine, job_id_of(engine, run.id))
    assert job["status"] == JobStatus.CANCELLED.value
    assert job["error_code"] == EXECUTION_CANCELLED
    assert stored(job["cancel_requested_at"]) == "2026-09-14 00:00:03"
    assert job["claimed_by"] is None and job["claim_token"] is None

    # No Attempt is invented for work that never began.
    assert counts(engine)["job_attempts"] == 0
    assert event_types(engine, run.id) == [
        "run.created",
        "run.queued",
        "cancellation.requested",
        "run.cancelled",
    ]


def test_a_cancelled_queued_job_can_never_be_claimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The durable proof that cancellation invokes the provider zero times."""
    engine, run = submitted(tmp_path, monkeypatch)

    assert (
        cancelling(engine).cancel_run(user_id=1, run_id=run.id, now=NOW + timedelta(seconds=1))
        is CancellationOutcome.CANCELLED
    )

    # Long after the original availability instant, no Worker can take it.
    assert claim_now(engine, now=NOW + timedelta(days=1)) is None
    assert counts(engine)["job_attempts"] == 0


# ---------------------------------------------------------------------------------------
# retry_wait: cancellation of a durable scheduled retry
# ---------------------------------------------------------------------------------------


def rate_limited(engine: Engine, claim: ClaimedAttempt, *, now: datetime) -> None:
    execution(engine).record_failure(
        claim,
        error_code=MODEL_RATE_LIMITED,
        error_message=safe_error_message(MODEL_RATE_LIMITED),
        retry_disposition=RetryDisposition.SAFE_TO_RETRY,
        usage=USAGE,
        elapsed_ms=9,
        anchor_at=now,
        retry_policy=PRODUCTION_RETRY_POLICY,
        now=now,
    )


def test_a_waiting_retry_is_cancelled_without_losing_its_start_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, run = submitted(tmp_path, monkeypatch)
    first = started(engine, now=NOW)
    rate_limited(engine, first, now=NOW + timedelta(seconds=1))
    job = job_row(engine, job_id_of(engine, run.id))
    assert job["status"] == JobStatus.RETRY_WAIT.value
    due_at = stored(job["available_at"])

    outcome = cancelling(engine).cancel_run(
        user_id=1, run_id=run.id, now=NOW + timedelta(seconds=2)
    )

    assert outcome is CancellationOutcome.CANCELLED
    row = run_row(engine, run.id)
    assert row["status"] == RunStatus.CANCELLED.value
    # The Run really had started, so its real start instant survives and the elapsed interval
    # describes the lifetime up to cancellation rather than a fabricated provider duration.
    assert stored(row["started_at"]) == stored(NOW)
    assert row["elapsed_ms"] == 2000
    assert row["output_text"] is None and row["error_code"] is None

    cancelled_job = job_row(engine, job_id_of(engine, run.id))
    assert cancelled_job["status"] == JobStatus.CANCELLED.value
    # History is append-only: the retry's due instant is not rewritten or erased.
    assert stored(cancelled_job["available_at"]) == due_at
    assert cancelled_job["claimed_by"] is None

    assert attempt_row(engine, first.attempt_id)["status"] == AttemptStatus.FAILED.value
    assert counts(engine)["job_attempts"] == 1


def test_a_cancelled_waiting_retry_never_executes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, run = submitted(tmp_path, monkeypatch)
    first = started(engine, now=NOW)
    rate_limited(engine, first, now=NOW + timedelta(seconds=1))
    due_at = job_row(engine, job_id_of(engine, run.id))["available_at"]

    cancelling(engine).cancel_run(user_id=1, run_id=run.id, now=NOW + timedelta(seconds=2))

    # Exactly at the due instant, and long after it, the retry is unreachable.
    assert claim_now(engine, now=datetime.fromisoformat(str(due_at)).replace(tzinfo=UTC)) is None
    assert claim_now(engine, now=NOW + timedelta(days=1)) is None
    assert counts(engine)["job_attempts"] == 1


# ---------------------------------------------------------------------------------------
# Claimed before execution: the boundary race
# ---------------------------------------------------------------------------------------


def test_a_claim_that_never_started_is_cancelled_pre_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, run = submitted(tmp_path, monkeypatch)
    claimed = claim_now(engine, now=NOW)
    assert claimed is not None

    outcome = cancelling(engine).cancel_run(
        user_id=1, run_id=run.id, now=NOW + timedelta(seconds=1)
    )

    assert outcome is CancellationOutcome.CANCELLED
    row = run_row(engine, run.id)
    assert row["status"] == RunStatus.CANCELLED.value
    assert row["started_at"] is None and row["elapsed_ms"] is None
    attempt = attempt_row(engine, claimed.attempt_id)
    assert attempt["status"] == AttemptStatus.CANCELLED.value
    # The pre-start cancellation must not fabricate the boundary it never crossed.
    assert attempt["execution_started_at"] is None


def test_cancelling_before_the_start_boundary_stops_the_provider_being_invoked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancel wins the race: the start transaction matches zero rows."""
    engine, run = submitted(tmp_path, monkeypatch)
    claimed = claim_now(engine, now=NOW)
    assert claimed is not None

    cancelling(engine).cancel_run(user_id=1, run_id=run.id, now=NOW + timedelta(seconds=1))

    assert execution(engine).start_attempt(claimed, now=NOW + timedelta(seconds=1)) is False
    assert run_row(engine, run.id)["started_at"] is None
    assert event_types(engine, run.id).count("attempt.started") == 0


def test_a_start_that_wins_the_race_makes_the_cancellation_post_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Start wins: cancellation still succeeds, but reports the real start boundary."""
    engine, run = submitted(tmp_path, monkeypatch)
    claimed = started(engine, now=NOW)

    outcome = cancelling(engine).cancel_run(
        user_id=1, run_id=run.id, now=NOW + timedelta(seconds=4)
    )

    assert outcome is CancellationOutcome.CANCELLED
    row = run_row(engine, run.id)
    assert row["status"] == RunStatus.CANCELLED.value
    assert stored(row["started_at"]) == stored(NOW)
    assert row["elapsed_ms"] == 4000
    assert attempt_row(engine, claimed.attempt_id)["status"] == AttemptStatus.CANCELLED.value


# ---------------------------------------------------------------------------------------
# Late writers: every path that could overwrite a cancellation
# ---------------------------------------------------------------------------------------


def test_a_cancelled_claim_loses_every_late_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old Worker token is powerless after the cancellation commits."""
    engine, run = submitted(tmp_path, monkeypatch)
    claimed = started(engine, now=NOW)
    cancelling(engine).cancel_run(user_id=1, run_id=run.id, now=NOW + timedelta(seconds=2))

    persistence = execution(engine)
    later = NOW + timedelta(seconds=3)
    assert persistence.renew_lease(claimed, now=later, lease_duration=LEASE_DURATION) is False
    assert (
        persistence.succeed(
            claimed,
            output_text="too late",
            finish_reason="stop",
            usage=USAGE,
            elapsed_ms=12,
            now=later,
        )
        is False
    )
    assert persistence.start_attempt(claimed, now=later) is False
    assert (
        persistence.record_failure(
            claimed,
            error_code=MODEL_RATE_LIMITED,
            error_message=safe_error_message(MODEL_RATE_LIMITED),
            retry_disposition=RetryDisposition.SAFE_TO_RETRY,
            usage=USAGE,
            elapsed_ms=12,
            anchor_at=later,
            retry_policy=PRODUCTION_RETRY_POLICY,
            now=later,
        )
        is not None
    )

    row = run_row(engine, run.id)
    assert row["status"] == RunStatus.CANCELLED.value
    assert row["output_text"] is None
    assert event_types(engine, run.id).count("run.succeeded") == 0
    assert event_types(engine, run.id).count("retry.scheduled") == 0


def test_a_success_that_committed_first_is_never_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, run = submitted(tmp_path, monkeypatch)
    claimed = started(engine, now=NOW)
    assert execution(engine).succeed(
        claimed,
        output_text="done",
        finish_reason="stop",
        usage=USAGE,
        elapsed_ms=7,
        now=NOW + timedelta(seconds=1),
    )

    outcome = cancelling(engine).cancel_run(
        user_id=1, run_id=run.id, now=NOW + timedelta(seconds=2)
    )

    assert outcome is CancellationOutcome.NOT_CANCELLABLE
    assert run_row(engine, run.id)["status"] == RunStatus.SUCCEEDED.value
    assert event_types(engine, run.id).count("run.cancelled") == 0


def test_a_failure_that_committed_first_is_never_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, run = submitted(tmp_path, monkeypatch)
    claimed = started(engine, now=NOW)
    execution(engine).record_failure(
        claimed,
        error_code=MODEL_UNAVAILABLE,
        error_message=safe_error_message(MODEL_UNAVAILABLE),
        retry_disposition=RetryDisposition.AMBIGUOUS,
        usage=USAGE,
        elapsed_ms=7,
        anchor_at=NOW + timedelta(seconds=1),
        retry_policy=PRODUCTION_RETRY_POLICY,
        now=NOW + timedelta(seconds=1),
    )

    outcome = cancelling(engine).cancel_run(
        user_id=1, run_id=run.id, now=NOW + timedelta(seconds=2)
    )

    assert outcome is CancellationOutcome.NOT_CANCELLABLE
    assert run_row(engine, run.id)["status"] == RunStatus.FAILED.value
    assert event_types(engine, run.id).count("run.cancelled") == 0


def test_cancellation_itself_committing_first_blocks_the_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mirror race: the Run fence is what makes this atomic, not timing."""
    engine, run = submitted(tmp_path, monkeypatch)
    claimed = started(engine, now=NOW)
    cancelling(engine).cancel_run(user_id=1, run_id=run.id, now=NOW + timedelta(seconds=2))

    assert (
        execution(engine).succeed(
            claimed,
            output_text="done",
            finish_reason="stop",
            usage=USAGE,
            elapsed_ms=7,
            now=NOW + timedelta(seconds=3),
        )
        is False
    )
    assert run_row(engine, run.id)["status"] == RunStatus.CANCELLED.value


def test_a_cancelled_job_can_never_schedule_a_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation revokes the permission to continue, so it revokes the successor too."""
    engine, run = submitted(tmp_path, monkeypatch)
    claimed = started(engine, now=NOW)
    cancelling(engine).cancel_run(user_id=1, run_id=run.id, now=NOW + timedelta(seconds=1))

    outcome = execution(engine).record_failure(
        claimed,
        error_code=MODEL_RATE_LIMITED,
        error_message=safe_error_message(MODEL_RATE_LIMITED),
        retry_disposition=RetryDisposition.SAFE_TO_RETRY,
        usage=USAGE,
        elapsed_ms=9,
        anchor_at=NOW + timedelta(seconds=2),
        retry_policy=PRODUCTION_RETRY_POLICY,
        now=NOW + timedelta(seconds=2),
    )

    assert outcome is not None
    assert job_row(engine, job_id_of(engine, run.id))["status"] == JobStatus.CANCELLED.value
    assert event_types(engine, run.id).count("retry.scheduled") == 0
    assert counts(engine)["job_attempts"] == 1


# ---------------------------------------------------------------------------------------
# Idempotency, ownership, and the readback the Worker relies on
# ---------------------------------------------------------------------------------------


def test_repeated_cancellation_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, run = submitted(tmp_path, monkeypatch)
    cancelling(engine).cancel_run(user_id=1, run_id=run.id, now=NOW + timedelta(seconds=3))
    before = run_row(engine, run.id)
    events = event_types(engine, run.id)

    again = cancelling(engine).cancel_run(user_id=1, run_id=run.id, now=NOW + timedelta(seconds=90))

    assert again is CancellationOutcome.CANCELLED
    assert run_row(engine, run.id) == before
    assert event_types(engine, run.id) == events
    assert stored(job_row(engine, job_id_of(engine, run.id))["cancel_requested_at"]) == (
        "2026-09-14 00:00:03"
    )


def test_a_foreign_user_cannot_cancel_and_cannot_discover_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, run = submitted(tmp_path, monkeypatch)

    assert (
        cancelling(engine).cancel_run(user_id=2, run_id=run.id, now=NOW)
        is CancellationOutcome.NOT_FOUND
    )
    assert (
        cancelling(engine).cancel_run(user_id=1, run_id=run.id + 999, now=NOW)
        is CancellationOutcome.NOT_FOUND
    )
    assert run_row(engine, run.id)["status"] == RunStatus.CREATED.value


def test_the_worker_reads_a_cancellation_apart_from_an_ordinary_terminal_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This distinction is what makes the Worker stop its local task instead of waiting."""
    engine, run = submitted(tmp_path, monkeypatch)
    claimed = started(engine, now=NOW)
    persistence = execution(engine)
    assert persistence.inspect_claim(claimed, now=NOW) is ClaimState.ACTIVE

    cancelling(engine).cancel_run(user_id=1, run_id=run.id, now=NOW + timedelta(seconds=1))

    assert (
        persistence.inspect_claim(claimed, now=NOW + timedelta(seconds=1)) is ClaimState.CANCELLED
    )


def test_a_cancelled_job_releases_its_queue_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, run = submitted(tmp_path, monkeypatch)
    job_id = job_id_of(engine, run.id)
    assert job_row(engine, job_id)["status"] in {
        JobStatus.QUEUED.value,
    }

    cancelling(engine).cancel_run(user_id=1, run_id=run.id, now=NOW)

    with engine.connect() as connection:
        occupying = connection.scalar(
            text(
                "SELECT count(*) FROM jobs WHERE status IN"
                " ('queued','claimed','running','retry_wait')"
            )
        )
    assert occupying == 0


def test_cancellation_survives_a_reload_of_the_durable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reopened engine sees the same cancelled Run: nothing lives only in memory."""
    engine, run = submitted(tmp_path, monkeypatch)
    cancelling(engine).cancel_run(user_id=1, run_id=run.id, now=NOW + timedelta(seconds=1))
    expected = run_row(engine, run.id)
    engine.dispose()

    reopened = create_sqlite_engine(tmp_path / "cancel.db")
    try:
        assert run_row(reopened, run.id) == expected
    finally:
        reopened.dispose()


def test_cancelling_a_legacy_run_with_no_job_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Run with no Job has no authority to revoke and no legal event to carry."""
    engine, run = submitted(tmp_path, monkeypatch)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM run_events WHERE run_id=:r"), {"r": run.id})
        connection.execute(
            text(
                "DELETE FROM jobs WHERE run_id=:r AND id NOT IN (SELECT job_id FROM job_attempts)"
            ),
            {"r": run.id},
        )

    outcome = cancelling(engine).cancel_run(user_id=1, run_id=run.id, now=NOW)

    assert outcome is CancellationOutcome.NOT_CANCELLABLE
    assert run_row(engine, run.id)["status"] == RunStatus.CREATED.value


def test_the_cancellation_code_is_never_a_provider_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`execution_cancelled` is infrastructure-owned and must not steer retry policy."""
    from nervos_core.application.job_execution import disposition_for

    assert disposition_for(EXECUTION_CANCELLED) is RetryDisposition.AMBIGUOUS
    assert disposition_for(EXECUTION_CANCELLED) is not RetryDisposition.SAFE_TO_RETRY
