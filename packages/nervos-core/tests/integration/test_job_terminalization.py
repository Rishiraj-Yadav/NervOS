"""C2 execution-start, lease-fencing, terminalization, and legacy closeout tests."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from execution_support import NOW, attempt_row, counts, event_types, job_id_of, job_row, migrate
from nervos_core.application.job_execution import LEASE_DURATION, ClaimedAttempt, ClaimState
from nervos_core.application.model_completion import (
    EXECUTION_OUTCOME_AMBIGUOUS,
    MODEL_RATE_LIMITED,
    safe_error_message,
)
from nervos_core.domain.jobs import AttemptStatus, JobStatus, RetryDisposition
from nervos_core.domain.runs import STAGE_B_LIMITS, ModelUsage, Run
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobExecutionPersistence,
    SqlAlchemyJobPersistence,
)
from sqlalchemy import Engine, text

USAGE = ModelUsage(11, 7, 18)


def prepared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str = "exec.db"
) -> tuple[Engine, Run]:
    """Return a migrated engine holding one queued Run and its Job."""
    engine = migrate(tmp_path / name, monkeypatch)
    run = SqlAlchemyJobPersistence(engine, max_pending=1000).submit(
        owner_user_id=1,
        agent_instance_id=1,
        input_text="hello",
        limits=STAGE_B_LIMITS,
        now=NOW,
    )
    return engine, run


def execution(engine: Engine) -> SqlAlchemyJobExecutionPersistence:
    return SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None)


def start(engine: Engine, *, now: datetime = NOW, worker_id: str = "worker-1") -> ClaimedAttempt:
    claimed = execution(engine).claim_next(
        worker_id=worker_id,
        provider_ids=("anthropic",),
        max_active=4,
        now=now,
        lease_duration=LEASE_DURATION,
    )
    assert claimed is not None
    assert execution(engine).start_attempt(claimed, now=now) is True
    return claimed


def test_execution_start_moves_every_record_and_appends_one_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, run = prepared(tmp_path, monkeypatch)
    try:
        claimed = start(engine)
        assert job_row(engine, claimed.job_id)["status"] == JobStatus.RUNNING.value
        attempt = attempt_row(engine, claimed.attempt_id)
        assert attempt["status"] == AttemptStatus.RUNNING.value
        assert attempt["execution_started_at"] is not None
        assert run_row_status(engine, claimed.run_id) == "running"
        assert event_types(engine, claimed.run_id)[-1] == "attempt.started"
        assert counts(engine)["run_events"] == 4
        del run
    finally:
        engine.dispose()


def run_row_status(engine: Engine, run_id: int) -> str:
    with engine.connect() as connection:
        return str(connection.scalar(text("SELECT status FROM runs WHERE id=:r"), {"r": run_id}))


def test_start_is_fenced_on_a_rotated_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = prepared(tmp_path, monkeypatch)
    try:
        claimed = execution(engine).claim_next(
            worker_id="worker-1",
            provider_ids=("anthropic",),
            max_active=4,
            now=NOW,
            lease_duration=LEASE_DURATION,
        )
        assert claimed is not None
        impostor = dataclasses.replace(claimed, claim_token=b"\x00" * 32)
        assert execution(engine).start_attempt(impostor, now=NOW) is False
        assert job_row(engine, claimed.job_id)["status"] == JobStatus.CLAIMED.value
        assert attempt_row(engine, claimed.attempt_id)["execution_started_at"] is None
        assert event_types(engine, claimed.run_id) == [
            "run.created",
            "run.queued",
            "attempt.claimed",
        ]
    finally:
        engine.dispose()


def test_start_is_fenced_on_an_expired_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = prepared(tmp_path, monkeypatch)
    try:
        claimed = execution(engine).claim_next(
            worker_id="worker-1",
            provider_ids=("anthropic",),
            max_active=4,
            now=NOW,
            lease_duration=LEASE_DURATION,
        )
        assert claimed is not None
        after = NOW + LEASE_DURATION + timedelta(seconds=1)
        assert execution(engine).start_attempt(claimed, now=after) is False
        assert job_row(engine, claimed.job_id)["status"] == JobStatus.CLAIMED.value
    finally:
        engine.dispose()


def test_success_terminalization_closes_all_three_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = prepared(tmp_path, monkeypatch)
    try:
        claimed = start(engine)
        assert (
            execution(engine).succeed(
                claimed,
                output_text="answer",
                finish_reason="stop",
                usage=USAGE,
                elapsed_ms=42,
                now=NOW + timedelta(seconds=2),
            )
            is True
        )
        job = job_row(engine, claimed.job_id)
        assert job["status"] == JobStatus.SUCCEEDED.value
        assert job["claimed_by"] is None and job["claim_token"] is None
        assert job["lease_expires_at"] is None and job["last_heartbeat_at"] is None
        attempt = attempt_row(engine, claimed.attempt_id)
        assert attempt["status"] == AttemptStatus.SUCCEEDED.value
        assert attempt["retry_disposition"] is None and attempt["error_code"] is None
        with engine.connect() as connection:
            run = dict(
                connection.execute(
                    text("SELECT status, output_text, finish_reason, elapsed_ms FROM runs")
                )
                .mappings()
                .one()
            )
        assert run["status"] == "succeeded"
        assert run["output_text"] == "answer"
        assert run["finish_reason"] == "stop"
        assert run["elapsed_ms"] == 42
        assert event_types(engine, claimed.run_id)[-1] == "run.succeeded"
    finally:
        engine.dispose()


def test_failure_terminalization_writes_two_consecutive_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = prepared(tmp_path, monkeypatch)
    try:
        claimed = start(engine)
        assert (
            execution(engine).fail(
                claimed,
                error_code=MODEL_RATE_LIMITED,
                error_message=safe_error_message(MODEL_RATE_LIMITED),
                retry_disposition=RetryDisposition.SAFE_TO_RETRY,
                usage=USAGE,
                elapsed_ms=17,
                now=NOW + timedelta(seconds=2),
            )
            is True
        )
        attempt = attempt_row(engine, claimed.attempt_id)
        assert attempt["status"] == AttemptStatus.FAILED.value
        assert attempt["retry_disposition"] == RetryDisposition.SAFE_TO_RETRY.value
        assert attempt["error_code"] == MODEL_RATE_LIMITED
        job = job_row(engine, claimed.job_id)
        assert job["status"] == JobStatus.FAILED.value
        assert job["error_code"] == MODEL_RATE_LIMITED

        with engine.connect() as connection:
            sequences = list(
                connection.execute(
                    text(
                        "SELECT sequence, event_type FROM run_events WHERE run_id=:r"
                        " ORDER BY sequence"
                    ),
                    {"r": claimed.run_id},
                )
            )
        assert sequences == [
            (1, "run.created"),
            (2, "run.queued"),
            (3, "attempt.claimed"),
            (4, "attempt.started"),
            (5, "attempt.failed"),
            (6, "run.failed"),
        ]
        # C2 records evidence and never acts on it: the Job is terminal, not retry_wait.
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT count(*) FROM jobs WHERE status='retry_wait'")) == 0
            )
        assert job["attempt_count"] == 1
    finally:
        engine.dispose()


def test_rotated_token_cannot_terminalize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, _ = prepared(tmp_path, monkeypatch)
    try:
        claimed = start(engine)
        impostor = dataclasses.replace(claimed, claim_token=b"\x01" * 32)
        assert (
            execution(engine).succeed(
                impostor,
                output_text="stolen",
                finish_reason="stop",
                usage=USAGE,
                elapsed_ms=1,
                now=NOW + timedelta(seconds=2),
            )
            is False
        )
        # The genuine authority survives byte-identically.
        assert job_row(engine, claimed.job_id)["status"] == JobStatus.RUNNING.value
        assert attempt_row(engine, claimed.attempt_id)["status"] == AttemptStatus.RUNNING.value
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT output_text FROM runs")) is None
    finally:
        engine.dispose()


def test_expired_lease_is_authority_loss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, _ = prepared(tmp_path, monkeypatch)
    try:
        claimed = start(engine)
        after = NOW + LEASE_DURATION + timedelta(seconds=1)
        repository = execution(engine)
        assert repository.renew_lease(claimed, now=after, lease_duration=LEASE_DURATION) is False
        assert (
            repository.succeed(
                claimed,
                output_text="late",
                finish_reason="stop",
                usage=USAGE,
                elapsed_ms=1,
                now=after,
            )
            is False
        )
        assert repository.inspect_claim(claimed, now=after) is ClaimState.LOST
        # The row is left exactly as it was, stranded for C3 — not requeued, not overwritten.
        assert job_row(engine, claimed.job_id)["status"] == JobStatus.RUNNING.value
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT output_text FROM runs")) is None
            assert connection.scalar(text("SELECT count(*) FROM jobs WHERE status='queued'")) == 0
    finally:
        engine.dispose()


def test_heartbeat_renews_both_rows_and_never_writes_an_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = prepared(tmp_path, monkeypatch)
    try:
        claimed = start(engine)
        repository = execution(engine)
        before = job_row(engine, claimed.job_id)["lease_expires_at"]
        assert before is not None
        later = NOW + timedelta(seconds=15)
        assert repository.renew_lease(claimed, now=later, lease_duration=LEASE_DURATION) is True
        after = job_row(engine, claimed.job_id)["lease_expires_at"]
        assert after is not None
        assert str(after) > str(before)
        assert attempt_row(engine, claimed.attempt_id)["execution_started_at"] is not None
        assert repository.inspect_claim(claimed, now=later) is ClaimState.ACTIVE
        assert counts(engine)["run_events"] == 4
    finally:
        engine.dispose()


def test_inspect_claim_reports_terminal_after_a_committed_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = prepared(tmp_path, monkeypatch)
    try:
        claimed = start(engine)
        repository = execution(engine)
        repository.succeed(
            claimed,
            output_text="done",
            finish_reason="stop",
            usage=USAGE,
            elapsed_ms=3,
            now=NOW + timedelta(seconds=1),
        )
        assert (
            repository.inspect_claim(claimed, now=NOW + timedelta(seconds=1)) is ClaimState.TERMINAL
        )
    finally:
        engine.dispose()


def test_heartbeat_after_terminalization_is_a_benign_zero_row_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = prepared(tmp_path, monkeypatch)
    try:
        claimed = start(engine)
        repository = execution(engine)
        repository.succeed(
            claimed,
            output_text="done",
            finish_reason="stop",
            usage=USAGE,
            elapsed_ms=3,
            now=NOW + timedelta(seconds=1),
        )
        assert (
            repository.renew_lease(
                claimed, now=NOW + timedelta(seconds=2), lease_duration=LEASE_DURATION
            )
            is False
        )
        assert job_row(engine, claimed.job_id)["status"] == JobStatus.SUCCEEDED.value
    finally:
        engine.dispose()


def test_legacy_running_run_without_a_job_is_closed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = prepared(tmp_path, monkeypatch)
    try:
        # Fabricate a legacy Stage B `running` Run: started, never terminalized, no Job.
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM run_events"))
            connection.execute(text("DELETE FROM jobs"))
            connection.execute(
                text("UPDATE runs SET status='running', started_at=:s"),
                {"s": NOW + timedelta(seconds=1)},
            )
        repository = execution(engine)
        later = NOW + timedelta(seconds=31)
        assert repository.close_legacy_nonterminal_runs(now=later) == 1
        with engine.connect() as connection:
            row = dict(
                connection.execute(
                    text(
                        "SELECT status, error_code, error_message, elapsed_ms, started_at,"
                        " finished_at FROM runs"
                    )
                )
                .mappings()
                .one()
            )
        assert row["status"] == "failed"
        assert row["error_code"] == EXECUTION_OUTCOME_AMBIGUOUS
        assert row["error_message"] == safe_error_message(EXECUTION_OUTCOME_AMBIGUOUS)
        assert row["elapsed_ms"] == 30_000
        assert row["started_at"] is not None
        # No Job, Attempt, or Event is ever synthesized for a legacy Run.
        assert counts(engine) == {"runs": 1, "jobs": 0, "job_attempts": 0, "run_events": 0}
        assert repository.close_legacy_nonterminal_runs(now=later) == 0
    finally:
        engine.dispose()


def test_legacy_created_run_without_a_job_is_left_exactly_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A never-started Run has no schema-legal terminal shape, so C2 does not invent one."""
    engine, _ = prepared(tmp_path, monkeypatch)
    try:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM run_events"))
            connection.execute(text("DELETE FROM jobs"))
        before = run_snapshot(engine)
        assert execution(engine).close_legacy_nonterminal_runs(now=NOW + timedelta(seconds=30)) == 0
        assert run_snapshot(engine) == before
        assert before["status"] == "created"
        assert before["started_at"] is None and before["elapsed_ms"] is None
    finally:
        engine.dispose()


def test_closeout_never_touches_a_run_that_has_a_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, run = prepared(tmp_path, monkeypatch)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE runs SET status='running', started_at=:s WHERE id=:r"),
                {"s": NOW + timedelta(seconds=1), "r": run.id},
            )
        assert execution(engine).close_legacy_nonterminal_runs(now=NOW + timedelta(seconds=5)) == 0
        assert run_row_status(engine, run.id) == "running"
        assert job_id_of(engine, run.id) > 0
    finally:
        engine.dispose()


def run_snapshot(engine: Engine) -> dict[str, object]:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text(
                    "SELECT status, started_at, finished_at, elapsed_ms, error_code,"
                    " error_message FROM runs"
                )
            )
            .mappings()
            .one()
        )
