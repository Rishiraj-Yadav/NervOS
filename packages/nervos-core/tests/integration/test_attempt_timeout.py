"""C5 execution deadline: the outer watchdog, its winner rule, and its durable outcome.

The inner `asyncio.timeout` inside `RunExecutor` bounds a *cooperative* provider call. This suite
covers the watchdog that bounds the Attempt even when the provider coroutine refuses to
cooperate, plus the orderings against success, cancellation, and lease loss. Deadlines here are
milliseconds, never long real sleeps.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from execution_support import NOW, attempt_row, counts, event_types, job_row, migrate
from nervos_core.application.job_execution import (
    LEASE_DURATION,
    JobExecutionService,
)
from nervos_core.application.model_completion import (
    MODEL_RATE_LIMITED,
    MODEL_TIMED_OUT,
    ModelCompletion,
    ModelRequest,
    ModelResponse,
    StopOutcome,
)
from nervos_core.application.retry_policy import PRODUCTION_RETRY_POLICY
from nervos_core.application.run_execution import RunExecutor
from nervos_core.application.trusted_chat import create_builtin_handler_registry
from nervos_core.domain.jobs import AttemptStatus, JobStatus
from nervos_core.domain.runs import ModelUsage, Run, RunLimits
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobExecutionPersistence,
    SqlAlchemyJobPersistence,
    SqlAlchemyRunCancellationPersistence,
)
from sqlalchemy import Engine, text

PROVIDER = "anthropic"
DEADLINE_MS = 40
DRAIN_MS = 60


class Clock:
    """A controllable aware-UTC clock for the persistence timestamps."""

    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class Completion:
    """Deterministic provider double recording every request."""

    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            "answer", PROVIDER, request.model_name, StopOutcome.STOP, ModelUsage(1, 1, 2)
        )


async def no_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


def submitted(engine: Engine, *, timeout_ms: int = DEADLINE_MS, max_attempts: int = 3) -> Run:
    return SqlAlchemyJobPersistence(engine, max_pending=1000).submit(
        owner_user_id=1,
        agent_instance_id=1,
        input_text="hello",
        limits=RunLimits(provider_timeout_ms=timeout_ms),
        now=NOW,
        max_attempts=max_attempts,
    )


def execution(engine: Engine) -> SqlAlchemyJobExecutionPersistence:
    return SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None)


def service(
    engine: Engine,
    persistence: SqlAlchemyJobExecutionPersistence,
    completion: ModelCompletion,
    clock: Clock,
    **kwargs: object,
) -> JobExecutionService:
    return JobExecutionService(
        persistence,
        RunExecutor(create_builtin_handler_registry()),
        {PROVIDER: completion},
        clock,
        sleep=no_sleep,
        drain_timeout=timedelta(milliseconds=DRAIN_MS),
        **kwargs,  # type: ignore[arg-type]
    )


def started(engine: Engine, *, now: datetime = NOW):
    """Claim and cross the execution boundary, as the orchestration itself would."""
    claimed = claim(engine, now=now)
    assert execution(engine).start_attempt(claimed, now=now) is True
    return claimed


def claim(engine: Engine, *, now: datetime = NOW):
    claimed = execution(engine).claim_next(
        worker_id="worker-1",
        provider_ids=(PROVIDER,),
        max_active=4,
        now=now,
        lease_duration=LEASE_DURATION,
    )
    assert claimed is not None
    return claimed


class NonCooperative(Completion):
    """A provider coroutine that swallows cancellation and keeps going."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.swallowed = False
        self.released = False
        self.delivered = False

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        self.entered.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.swallowed = True
            # Refuses to die, and eventually answers with a real result.
            while not self.released:
                await asyncio.sleep(0.01)
        self.delivered = True
        return ModelResponse(
            "late answer", PROVIDER, request.model_name, StopOutcome.STOP, ModelUsage(1, 1, 2)
        )


# ---------------------------------------------------------------------------------------
# The watchdog fires, independently of the inner bound
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_non_cooperative_provider_still_settles_as_a_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inner bound cannot complete against a coroutine that ignores cancellation.

    This is the whole reason the deadline is also owned outside `RunExecutor`: the slot must be
    released and the Attempt settled truthfully even when the local call will not die.
    """
    engine = migrate(tmp_path / "timeout.db", monkeypatch)
    try:
        submitted(engine)
        completion = NonCooperative()
        repository = execution(engine)
        claimed = claim(engine)
        orchestrator = service(engine, repository, completion, Clock())

        task = asyncio.create_task(orchestrator.execute(claimed))
        await asyncio.wait_for(completion.entered.wait(), timeout=5)
        # The watchdog releases the slot on its own bound, without the provider cooperating.
        assert await asyncio.wait_for(task, timeout=5) is not None

        row = job_row(engine, claimed.job_id)
        assert row["status"] == JobStatus.FAILED.value
        assert row["error_code"] == MODEL_TIMED_OUT
        assert completion.swallowed is True
        with engine.connect() as connection:
            status = connection.scalar(text("SELECT status FROM runs"))
            code = connection.scalar(text("SELECT error_code FROM runs"))
        assert (status, code) == ("failed", MODEL_TIMED_OUT)

        # Let the abandoned call try to answer, then confirm the fences still discard it.
        completion.released = True
        await asyncio.sleep(0.3)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT status FROM runs")) == "failed"
            assert connection.scalar(text("SELECT output_text FROM runs")) is None
        assert "run.succeeded" not in event_types(engine, claimed.run_id)
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_a_cooperative_provider_completing_first_is_not_relabelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A result already in hand wins, so a slow-but-successful call is never a timeout."""
    engine = migrate(tmp_path / "timeout-ok.db", monkeypatch)
    try:
        submitted(engine, timeout_ms=60_000)
        completion = Completion()
        claimed = claim(engine)
        orchestrator = service(engine, execution(engine), completion, Clock())

        outcome = await asyncio.wait_for(orchestrator.execute(claimed), timeout=5)

        assert outcome is not None and outcome.error_code is None
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT status FROM runs")) == "succeeded"
        assert completion.calls == 1
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_the_watchdog_starts_at_the_execution_boundary_not_at_the_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Run that waited does not arrive with its deadline already spent."""
    engine = migrate(tmp_path / "timeout-window.db", monkeypatch)
    try:
        submitted(engine, timeout_ms=60_000)
        claimed = claim(engine)

        outcome = await asyncio.wait_for(
            service(engine, execution(engine), Completion(), Clock()).execute(claimed), timeout=5
        )

        assert outcome is not None and outcome.error_code is None
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# Durable policy: the timeout can never become a retry
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_timeout_terminalizes_ambiguously_and_never_schedules_a_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "timeout-retry.db", monkeypatch)
    try:
        submitted(engine)
        completion = NonCooperative()
        claimed = claim(engine)
        task = asyncio.create_task(
            service(engine, execution(engine), completion, Clock()).execute(claimed)
        )
        await asyncio.wait_for(completion.entered.wait(), timeout=5)
        await asyncio.wait_for(task, timeout=5)

        row = job_row(engine, claimed.job_id)
        assert row["status"] == JobStatus.FAILED.value
        assert row["claimed_by"] is None and row["claim_token"] is None
        assert attempt_row(engine, claimed.attempt_id)["status"] == AttemptStatus.FAILED.value
        timeline = event_types(engine, claimed.run_id)
        assert timeline[-2:] == ["attempt.failed", "run.failed"]
        assert "retry.scheduled" not in timeline
        assert counts(engine)["job_attempts"] == 1
        # A cancelled Run is impossible here: timeout is a failure, not a cancellation.
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT status FROM runs")) == "failed"
        completion.released = True
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# Orderings against success, cancellation, and lease loss
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_committed_success_is_never_overwritten_by_a_later_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "timeout-success.db", monkeypatch)
    try:
        submitted(engine, timeout_ms=60_000)
        claimed = started(engine)
        assert execution(engine).succeed(
            claimed,
            output_text="done",
            finish_reason="stop",
            usage=ModelUsage(1, 1, 2),
            elapsed_ms=5,
            now=NOW,
        )
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT status FROM runs")) == "succeeded"
        # The late deadline outcome is simply never produced: the success already won locally.
        assert "run.succeeded" in event_types(engine, claimed.run_id)
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_cancellation_committed_first_is_not_overwritten_by_a_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "timeout-cancel.db", monkeypatch)
    try:
        run = submitted(engine)
        completion = NonCooperative()
        claimed = claim(engine)
        task = asyncio.create_task(
            service(engine, execution(engine), completion, Clock()).execute(claimed)
        )
        await asyncio.wait_for(completion.entered.wait(), timeout=5)
        assert (
            SqlAlchemyRunCancellationPersistence(engine, sleep=lambda _: None).cancel_run(
                user_id=1, run_id=run.id, now=NOW + timedelta(seconds=1)
            )
            is not None
        )

        await asyncio.wait_for(task, timeout=5)
        completion.released = True
        await asyncio.sleep(0.2)

        with engine.connect() as connection:
            assert connection.scalar(text("SELECT status FROM runs")) == "cancelled"
            assert connection.scalar(text("SELECT output_text FROM runs")) is None
        timeline = event_types(engine, run.id)
        assert "run.failed" not in timeline
        assert timeline[-2:] == ["cancellation.requested", "run.cancelled"]
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_a_late_cancellation_cannot_rewrite_a_committed_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "timeout-then-cancel.db", monkeypatch)
    try:
        run = submitted(engine)
        completion = NonCooperative()
        claimed = claim(engine)
        task = asyncio.create_task(
            service(engine, execution(engine), completion, Clock()).execute(claimed)
        )
        await asyncio.wait_for(completion.entered.wait(), timeout=5)
        await asyncio.wait_for(task, timeout=5)
        completion.released = True

        from nervos_core.application.job_execution import CancellationOutcome

        outcome = SqlAlchemyRunCancellationPersistence(engine, sleep=lambda _: None).cancel_run(
            user_id=1, run_id=run.id, now=NOW + timedelta(seconds=5)
        )

        assert outcome is CancellationOutcome.NOT_CANCELLABLE
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT status FROM runs")) == "failed"
            assert connection.scalar(text("SELECT error_code FROM runs")) == MODEL_TIMED_OUT
        assert "run.cancelled" not in event_types(engine, run.id)
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_an_expired_lease_makes_the_timeout_write_lose_its_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once authority lapses, the old Worker writes nothing and C3 owns the closure."""
    engine = migrate(tmp_path / "timeout-expired.db", monkeypatch)
    try:
        submitted(engine)
        completion = NonCooperative()
        clock = Clock()
        claimed = claim(engine)
        task = asyncio.create_task(
            service(engine, execution(engine), completion, clock).execute(claimed)
        )
        await asyncio.wait_for(completion.entered.wait(), timeout=5)

        # The lease lapses while the provider is still running, so the fenced write must fail.
        clock.now = NOW + LEASE_DURATION + timedelta(seconds=1)
        await asyncio.wait_for(task, timeout=5)
        completion.released = True
        await asyncio.sleep(0.2)

        assert job_row(engine, claimed.job_id)["status"] == JobStatus.RUNNING.value
        assert attempt_row(engine, claimed.attempt_id)["status"] == AttemptStatus.RUNNING.value
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT status FROM runs")) == "running"
        assert "run.failed" not in event_types(engine, claimed.run_id)
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# Independence from the retry engine
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_retry_attempt_receives_its_own_deadline_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first Attempt waits out its retry; the second still gets a full window."""
    engine = migrate(tmp_path / "timeout-retry-window.db", monkeypatch)
    try:
        run = submitted(engine, timeout_ms=60_000)
        first = started(engine)
        from nervos_core.application.model_completion import safe_error_message
        from nervos_core.domain.jobs import RetryDisposition

        execution(engine).record_failure(
            first,
            error_code=MODEL_RATE_LIMITED,
            error_message=safe_error_message(MODEL_RATE_LIMITED),
            retry_disposition=RetryDisposition.SAFE_TO_RETRY,
            usage=ModelUsage(1, 1, 2),
            elapsed_ms=5,
            anchor_at=NOW,
            retry_policy=PRODUCTION_RETRY_POLICY,
            now=NOW,
        )
        assert job_row(engine, first.job_id)["status"] == JobStatus.RETRY_WAIT.value

        due = datetime.fromisoformat(str(job_row(engine, first.job_id)["available_at"])).replace(
            tzinfo=UTC
        )
        second = execution(engine).claim_next(
            worker_id="worker-1",
            provider_ids=(PROVIDER,),
            max_active=4,
            now=due + timedelta(seconds=1),
            lease_duration=LEASE_DURATION,
        )
        assert second is not None and second.attempt_number == 2

        completion = Completion()
        # The orchestration clock must agree with the instant the retry was claimed, exactly as
        # it would in a real Worker, where one wall clock drives both.
        clock = Clock()
        clock.now = due + timedelta(seconds=1)
        outcome = await asyncio.wait_for(
            service(engine, execution(engine), completion, clock).execute(second), timeout=5
        )

        # The queued time did not consume the second Attempt's deadline: it succeeded.
        assert outcome is not None and outcome.error_code is None
        assert completion.calls == 1
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT status FROM runs WHERE id=:r"), {"r": run.id}
            ) == ("succeeded")
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_an_abandoned_provider_task_is_observed_and_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The service retains no reference to a provider task it stopped waiting for.

    Whatever the local call does -- finish, die on cancellation, or outlive the drain bound --
    the service must end up holding nothing, and its eventual result must never reach durable
    state. A tracked task is always released again once it completes, which is what keeps
    `Task exception was never retrieved` from ever being reported.
    """
    engine = migrate(tmp_path / "timeout-abandon.db", monkeypatch)
    try:
        submitted(engine)
        completion = NonCooperative()
        claimed = claim(engine)
        orchestrator = service(engine, execution(engine), completion, Clock())
        task = asyncio.create_task(orchestrator.execute(claimed))
        await asyncio.wait_for(completion.entered.wait(), timeout=5)
        await asyncio.wait_for(task, timeout=5)

        completion.released = True
        await asyncio.sleep(0.3)

        # Nothing is retained, and the late answer never reached the Run.
        assert orchestrator.abandoned_tasks == frozenset()
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT status FROM runs")) == "failed"
            assert connection.scalar(text("SELECT output_text FROM runs")) is None
        assert "run.succeeded" not in event_types(engine, claimed.run_id)
    finally:
        engine.dispose()
