"""C2 Worker loop: capability filtering, one-shot execution, and graceful shutdown.

These tests drive the *shipped* `Worker` loop over disposable persistence, so the claim,
lease, heartbeat, and terminalization behaviour asserted here is the behaviour a running
Worker process performs.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path

import pytest
from nervos_core.application.model_completion import (
    MODEL_RATE_LIMITED,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
)
from nervos_core.domain.jobs import AttemptStatus, JobStatus, RetryDisposition
from sqlalchemy import text
from support import (
    NOW,
    PROVIDER_ID,
    SECOND_PROVIDER_ID,
    RecordingCompletion,
    attempt_rows,
    build_worker,
    counts,
    event_types,
    job_row,
    migrate,
    run_row,
    run_until_stopped,
    submit,
)


@pytest.mark.anyio
async def test_a_queued_run_is_claimed_started_executed_and_terminalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "worker.db", monkeypatch)
    try:
        run_id = submit(engine)
        completion = RecordingCompletion()
        worker = build_worker(engine, {PROVIDER_ID: completion})

        await run_until_stopped(worker, engine)

        assert completion.calls == 1
        row = run_row(engine, run_id)
        assert row["status"] == "succeeded"
        assert row["output_text"] == "worker answer"
        assert row["finish_reason"] == "stop"
        assert row["elapsed_ms"] is not None
        assert job_row(engine, run_id)["status"] == JobStatus.SUCCEEDED.value
        assert attempt_rows(engine) == [
            {
                "attempt_number": 1,
                "status": AttemptStatus.SUCCEEDED.value,
                "retry_disposition": None,
                "error_code": None,
            }
        ]
        assert event_types(engine, run_id) == [
            "run.created",
            "run.queued",
            "attempt.claimed",
            "attempt.started",
            "run.succeeded",
        ]
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_a_normalized_provider_failure_is_recorded_and_never_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "worker.db", monkeypatch)
    try:
        run_id = submit(engine)
        completion = RecordingCompletion()
        completion.error = ModelProviderError(MODEL_RATE_LIMITED)
        worker = build_worker(engine, {PROVIDER_ID: completion})

        await run_until_stopped(worker, engine)

        assert completion.calls == 1
        row = run_row(engine, run_id)
        assert row["status"] == "failed"
        assert row["error_code"] == MODEL_RATE_LIMITED
        assert row["error_message"]
        assert job_row(engine, run_id)["status"] == JobStatus.FAILED.value
        assert job_row(engine, run_id)["attempt_count"] == 1
        assert attempt_rows(engine)[0]["retry_disposition"] == RetryDisposition.SAFE_TO_RETRY.value
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT count(*) FROM jobs WHERE status='retry_wait'")) == 0
            )
        assert event_types(engine, run_id)[-2:] == ["attempt.failed", "run.failed"]
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_a_worker_without_the_provider_leaves_the_job_queued_and_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capability absence is a queue state, never an execution failure."""
    engine = migrate(tmp_path / "worker.db", monkeypatch)
    try:
        run_id = submit(engine)
        wrong = RecordingCompletion(provider_id=SECOND_PROVIDER_ID)
        worker = build_worker(engine, {SECOND_PROVIDER_ID: wrong})

        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(stop))
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

        assert wrong.calls == 0
        job = job_row(engine, run_id)
        assert job["status"] == JobStatus.QUEUED.value
        assert job["attempt_count"] == 0
        assert counts(engine)["job_attempts"] == 0
        assert event_types(engine, run_id) == ["run.created", "run.queued"]
        assert run_row(engine, run_id)["status"] == "created"
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_a_zero_provider_worker_claims_nothing_and_does_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "worker.db", monkeypatch)
    try:
        run_id = submit(engine)
        worker = build_worker(engine, {})

        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(stop))
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

        assert worker.provider_ids == ()
        assert counts(engine)["job_attempts"] == 0
        assert job_row(engine, run_id)["status"] == JobStatus.QUEUED.value
        assert run_row(engine, run_id)["status"] == "created"
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_capability_filtering_skips_an_ineligible_older_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Selection is the oldest *eligible* Job, not global FIFO across providers."""
    engine = migrate(tmp_path / "worker.db", monkeypatch)
    try:
        first = submit(engine, text_value="older")
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE runs SET model_provider=:p WHERE id=:r"),
                {"p": SECOND_PROVIDER_ID, "r": first},
            )
            connection.execute(
                text("UPDATE jobs SET model_provider=:p WHERE run_id=:r"),
                {"p": SECOND_PROVIDER_ID, "r": first},
            )
        second = submit(engine, text_value="newer")
        completion = RecordingCompletion()
        worker = build_worker(engine, {PROVIDER_ID: completion})

        await run_until_stopped(worker, engine)

        assert completion.calls == 1
        assert run_row(engine, second)["status"] == "succeeded"
        assert job_row(engine, first)["status"] == JobStatus.QUEUED.value
        assert job_row(engine, first)["attempt_count"] == 0
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_execution_uses_the_durable_snapshot_not_mutable_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "worker.db", monkeypatch)
    try:
        run_id = submit(engine)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE agent_instances SET model_name='changed/model',"
                    " model_provider=:p WHERE id=1"
                ),
                {"p": SECOND_PROVIDER_ID},
            )
        completion = RecordingCompletion()
        worker = build_worker(engine, {PROVIDER_ID: completion})

        await run_until_stopped(worker, engine)

        assert completion.calls == 1
        assert completion.requests[0].model_name == "opaque/model"
        assert run_row(engine, run_id)["status"] == "succeeded"
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_graceful_shutdown_keeps_the_lease_and_writes_no_terminal_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Worker that stops mid-execution never fabricates a result or a failure."""
    engine = migrate(tmp_path / "worker.db", monkeypatch)
    try:
        run_id = submit(engine)
        started = asyncio.Event()

        class Blocking(RecordingCompletion):
            async def complete(self, request: ModelRequest) -> ModelResponse:
                started.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        worker = build_worker(
            engine, {PROVIDER_ID: Blocking()}, shutdown_grace=0.2, poll_interval=0.01
        )
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(stop))
        await asyncio.wait_for(started.wait(), timeout=5)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

        # The Attempt is left running with an expiring lease: C3 reconciles it, C2 does not.
        assert job_row(engine, run_id)["status"] == JobStatus.RUNNING.value
        assert run_row(engine, run_id)["status"] == "running"
        assert run_row(engine, run_id)["error_code"] is None
        assert run_row(engine, run_id)["output_text"] is None
        assert attempt_rows(engine)[0]["status"] == AttemptStatus.RUNNING.value
        assert event_types(engine, run_id)[-1] == "attempt.started"
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_shutdown_stops_claiming_new_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "worker.db", monkeypatch)
    try:
        submit(engine, text_value="one")
        submit(engine, text_value="two")
        completion = RecordingCompletion()
        worker = build_worker(engine, {PROVIDER_ID: completion}, concurrency=1)

        stop = asyncio.Event()
        stop.set()
        await asyncio.wait_for(worker.run(stop), timeout=5)

        assert completion.calls == 0
        assert counts(engine)["job_attempts"] == 0
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_a_claim_token_never_reaches_a_log_record_or_a_run_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The claim token is bearer authority: real in memory, absent from every log and event."""
    sentinel = "SENTINEL-CLAIM-TOKEN-LEAK-000000"
    assert len(sentinel) == 32

    def fixed_token(size: int) -> bytes:
        return sentinel.encode("ascii")[:size]

    engine = migrate(tmp_path / "token.db", monkeypatch)
    try:
        monkeypatch.setattr(
            "nervos_core.infrastructure.database.jobs.os.urandom",
            fixed_token,
        )
        run_id = submit(engine)
        completion = RecordingCompletion()
        worker = build_worker(engine, {PROVIDER_ID: completion})
        with caplog.at_level(logging.DEBUG):
            await run_until_stopped(worker, engine)

        assert completion.calls == 1
        assert run_row(engine, run_id)["status"] == "succeeded"
        # Nothing on the claim, start, heartbeat, or terminalization path logged the token.
        assert sentinel not in caplog.text
        with engine.connect() as connection:
            events = connection.execute(
                text("SELECT event_type, code, message FROM run_events")
            ).all()
            job_token = connection.scalar(text("SELECT claim_token FROM jobs"))
            attempt_token = connection.scalar(text("SELECT claim_token FROM job_attempts"))
        assert len(events) == 5
        assert sentinel not in str(events)
        # The Job releases its claim on terminalization, as the frozen domain requires; the
        # Attempt keeps its 32-byte token because the domain requires every Attempt to carry
        # one. That retained value grants no authority: every fenced write needs the Job to be
        # `claimed`/`running`, so a terminal Attempt's token can never be used again.
        assert job_token is None
        assert attempt_token == sentinel.encode("ascii")
    finally:
        engine.dispose()


def test_the_worker_refuses_invalid_execution_limits() -> None:
    with pytest.raises(ValueError):
        build_worker(None, {}, concurrency=0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        build_worker(None, {}, max_active=99)  # type: ignore[arg-type]


def test_the_lease_window_outlives_a_slow_provider_call() -> None:
    from nervos_core.application.job_execution import HEARTBEAT_INTERVAL, LEASE_DURATION

    assert LEASE_DURATION >= 3 * HEARTBEAT_INTERVAL
    assert timedelta(seconds=30) > HEARTBEAT_INTERVAL
    assert NOW.tzinfo is not None
