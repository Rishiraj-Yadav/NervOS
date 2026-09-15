"""C2 JobExecutionService orchestration tests over the real durable persistence."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from execution_support import NOW, attempt_row, counts, event_types, job_row, migrate
from nervos_core.application.errors import PersistenceContention, PersistenceUnavailable
from nervos_core.application.job_execution import (
    LEASE_DURATION,
    ClaimedAttempt,
    ClaimState,
    JobExecutionPersistence,
    JobExecutionService,
    disposition_for,
)
from nervos_core.application.model_completion import (
    INTERNAL_EXECUTION_ERROR,
    MODEL_RATE_LIMITED,
    MODEL_REFUSED,
    MODEL_TIMED_OUT,
    MODEL_UNAVAILABLE,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    StopOutcome,
)
from nervos_core.application.run_execution import RunExecutor
from nervos_core.application.trusted_chat import create_builtin_handler_registry
from nervos_core.domain.jobs import AttemptStatus, JobStatus, RetryDisposition
from nervos_core.domain.runs import STAGE_B_LIMITS, ModelUsage
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobExecutionPersistence,
    SqlAlchemyJobPersistence,
)
from sqlalchemy import Engine, text

PROVIDER = "anthropic"


class MutableClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value = self.value + timedelta(seconds=seconds)


class Completion:
    """Deterministic completion double; records every call."""

    def __init__(self) -> None:
        self.calls = 0
        self.requests: list[ModelRequest] = []
        self.error: Exception | None = None

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return ModelResponse(
            "answer", PROVIDER, request.model_name, StopOutcome.STOP, ModelUsage(11, 7, 18)
        )


async def no_sleep(_seconds: float) -> None:
    return None


def prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str = "exec.db") -> Engine:
    engine = migrate(tmp_path / name, monkeypatch)
    SqlAlchemyJobPersistence(engine, max_pending=1000).submit(
        owner_user_id=1,
        agent_instance_id=1,
        input_text="hello",
        limits=STAGE_B_LIMITS,
        now=NOW,
    )
    return engine


def claim(engine: Engine, *, worker_id: str = "worker-1") -> ClaimedAttempt:
    claimed = SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None).claim_next(
        worker_id=worker_id,
        provider_ids=(PROVIDER,),
        max_active=4,
        now=NOW,
        lease_duration=LEASE_DURATION,
    )
    assert claimed is not None
    return claimed


def service(
    engine: Engine,
    persistence: JobExecutionPersistence,
    completion: Completion,
    clock: MutableClock,
    **kwargs: object,
) -> JobExecutionService:
    executor = RunExecutor(create_builtin_handler_registry())
    return JobExecutionService(
        persistence,
        executor,
        {PROVIDER: completion},
        clock,
        sleep=no_sleep,
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.anyio
async def test_success_persists_one_terminal_run_with_one_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = prepare(tmp_path, monkeypatch)
    try:
        clock = MutableClock()
        completion = Completion()
        repository = SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None)
        claimed = claim(engine)
        outcome = await service(engine, repository, completion, clock).execute(claimed)

        assert outcome is not None and outcome.status == "succeeded"
        assert completion.calls == 1
        assert completion.requests[0].model_name == "opaque/model"
        assert completion.requests[0].max_output_tokens == STAGE_B_LIMITS.max_output_tokens
        assert job_row(engine, claimed.job_id)["status"] == JobStatus.SUCCEEDED.value
        assert attempt_row(engine, claimed.attempt_id)["status"] == AttemptStatus.SUCCEEDED.value
        with engine.connect() as connection:
            run = dict(
                connection.execute(text("SELECT status, output_text, elapsed_ms FROM runs"))
                .mappings()
                .one()
            )
        assert run == {"status": "succeeded", "output_text": "answer", "elapsed_ms": 0}
        assert event_types(engine, claimed.run_id) == [
            "run.created",
            "run.queued",
            "attempt.claimed",
            "attempt.started",
            "run.succeeded",
        ]
    finally:
        engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (MODEL_RATE_LIMITED, RetryDisposition.SAFE_TO_RETRY),
        (MODEL_TIMED_OUT, RetryDisposition.AMBIGUOUS),
        (MODEL_UNAVAILABLE, RetryDisposition.AMBIGUOUS),
        (INTERNAL_EXECUTION_ERROR, RetryDisposition.AMBIGUOUS),
        (MODEL_REFUSED, RetryDisposition.DO_NOT_RETRY),
    ],
)
async def test_failure_records_the_disposition_and_never_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str, expected: RetryDisposition
) -> None:
    engine = prepare(tmp_path, monkeypatch)
    try:
        clock = MutableClock()
        completion = Completion()
        completion.error = ModelProviderError(code)
        repository = SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None)
        claimed = claim(engine)
        outcome = await service(engine, repository, completion, clock).execute(claimed)

        assert outcome is not None and outcome.status == "failed"
        assert outcome.error_code == code
        assert completion.calls == 1
        attempt = attempt_row(engine, claimed.attempt_id)
        assert attempt["status"] == AttemptStatus.FAILED.value
        assert attempt["retry_disposition"] == expected.value
        assert attempt["error_code"] == code
        job = job_row(engine, claimed.job_id)
        assert job["status"] == JobStatus.FAILED.value
        assert job["attempt_count"] == 1
        assert counts(engine)["job_attempts"] == 1
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT count(*) FROM jobs WHERE status='retry_wait'")) == 0
            )
            error_message = connection.scalar(text("SELECT error_message FROM runs"))
        assert isinstance(error_message, str) and 0 < len(error_message) <= 512
        assert event_types(engine, claimed.run_id)[-2:] == ["attempt.failed", "run.failed"]
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_disposition_policy_fails_closed_for_an_unknown_code() -> None:
    assert disposition_for("something_unrecognized") is RetryDisposition.AMBIGUOUS


@pytest.mark.anyio
async def test_start_boundary_failure_makes_zero_provider_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = prepare(tmp_path, monkeypatch)
    try:
        clock = MutableClock()
        completion = Completion()
        repository = SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None)
        claimed = claim(engine)
        # Authority is gone before the start boundary: the lease has already expired.
        clock.advance(LEASE_DURATION.total_seconds() + 1)
        outcome = await service(engine, repository, completion, clock).execute(claimed)

        assert outcome is None
        assert completion.calls == 0
        assert job_row(engine, claimed.job_id)["status"] == JobStatus.CLAIMED.value
        assert attempt_row(engine, claimed.attempt_id)["execution_started_at"] is None
        assert event_types(engine, claimed.run_id) == [
            "run.created",
            "run.queued",
            "attempt.claimed",
        ]
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_authority_lost_during_execution_discards_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An expired Worker must never commit a result it computed after losing authority."""
    engine = prepare(tmp_path, monkeypatch)
    try:
        clock = MutableClock()
        completion = Completion()
        repository = SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None)
        claimed = claim(engine)

        class ExpiringService(JobExecutionService):
            def __init__(self, *args: object, **kwargs: object) -> None:
                super().__init__(*args, **kwargs)  # type: ignore[arg-type]

            def _terminalize(self, claim: ClaimedAttempt, outcome: object) -> bool:
                # Every fenced write is now past the lease deadline.
                clock.advance(LEASE_DURATION.total_seconds() * 2)
                return super()._terminalize(claim, outcome)  # type: ignore[arg-type]

        orchestrator = ExpiringService(
            repository,
            RunExecutor(create_builtin_handler_registry()),
            {PROVIDER: completion},
            clock,
            sleep=no_sleep,
        )
        outcome = await orchestrator.execute(claimed)

        assert outcome is not None and outcome.status == "succeeded"
        assert completion.calls == 1
        # Nothing was written: the row stays exactly as it was, stranded for C3.
        assert job_row(engine, claimed.job_id)["status"] == JobStatus.RUNNING.value
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT output_text FROM runs")) is None
            assert connection.scalar(text("SELECT status FROM runs")) == "running"
        assert event_types(engine, claimed.run_id) == [
            "run.created",
            "run.queued",
            "attempt.claimed",
            "attempt.started",
        ]
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_persistence_retry_never_invokes_the_provider_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry loop replays only the terminal write; the provider is called exactly once."""
    engine = prepare(tmp_path, monkeypatch)
    try:
        clock = MutableClock()
        completion = Completion()
        repository = SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None)
        claimed = claim(engine)

        class FlakyPersistence:
            """Fails the terminal write twice with a *proven* contention, then delegates."""

            def __init__(self) -> None:
                self.terminal_attempts = 0

            def __getattr__(self, name: str) -> object:
                return getattr(repository, name)

            def succeed(self, *args: object, **kwargs: object) -> bool:
                self.terminal_attempts += 1
                if self.terminal_attempts <= 2:
                    raise PersistenceContention
                return repository.succeed(*args, **kwargs)  # type: ignore[arg-type]

        flaky = FlakyPersistence()
        outcome = await service(
            engine, cast(JobExecutionPersistence, flaky), completion, clock
        ).execute(claimed)

        assert outcome is not None and outcome.status == "succeeded"
        assert completion.calls == 1
        assert flaky.terminal_attempts == 3
        assert job_row(engine, claimed.job_id)["status"] == JobStatus.SUCCEEDED.value
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_exhausted_persistence_retry_discards_without_faking_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = prepare(tmp_path, monkeypatch)
    try:
        clock = MutableClock()
        completion = Completion()
        repository = SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None)
        claimed = claim(engine)

        class AlwaysFlaky:
            def __init__(self) -> None:
                self.terminal_attempts = 0

            def __getattr__(self, name: str) -> object:
                return getattr(repository, name)

            def succeed(self, *args: object, **kwargs: object) -> bool:
                self.terminal_attempts += 1
                raise PersistenceContention

        flaky = AlwaysFlaky()
        outcome = await service(
            engine, cast(JobExecutionPersistence, flaky), completion, clock
        ).execute(claimed)

        assert outcome is not None
        assert completion.calls == 1
        assert flaky.terminal_attempts == 5
        # The provider result is discarded and no terminal failure is fabricated.
        assert job_row(engine, claimed.job_id)["status"] == JobStatus.RUNNING.value
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT status FROM runs")) == "running"
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_unknown_persistence_failure_is_reconciled_by_reading_durable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unrecognized write failure is never replayed blindly; state is read back first."""
    engine = prepare(tmp_path, monkeypatch)
    try:
        clock = MutableClock()
        completion = Completion()
        repository = SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None)
        claimed = claim(engine)

        class Uncertain:
            def __init__(self) -> None:
                self.terminal_attempts = 0

            def __getattr__(self, name: str) -> object:
                return getattr(repository, name)

            def succeed(self, *args: object, **kwargs: object) -> bool:
                self.terminal_attempts += 1
                if self.terminal_attempts == 1:
                    raise PersistenceUnavailable
                return repository.succeed(*args, **kwargs)  # type: ignore[arg-type]

        uncertain = Uncertain()
        outcome = await service(
            engine, cast(JobExecutionPersistence, uncertain), completion, clock
        ).execute(claimed)

        assert outcome is not None
        assert completion.calls == 1
        # The first attempt is uncertain, the read-back proves the work is still ours and
        # uncommitted, so the second attempt commits exactly once.
        assert uncertain.terminal_attempts == 2
        assert job_row(engine, claimed.job_id)["status"] == JobStatus.SUCCEEDED.value
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_heartbeat_keeps_renewing_while_the_provider_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = prepare(tmp_path, monkeypatch)
    try:
        clock = MutableClock()
        started = asyncio.Event()
        release = asyncio.Event()

        class Blocking(Completion):
            async def complete(self, request: ModelRequest) -> ModelResponse:
                started.set()
                await release.wait()
                return await super().complete(request)

        blocking = Blocking()
        repository = SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None)
        claimed = claim(engine)
        orchestrator = service(
            engine,
            repository,
            blocking,
            clock,
            heartbeat_interval=timedelta(seconds=0.01),
        )
        task = asyncio.create_task(orchestrator.execute(claimed))
        await asyncio.wait_for(started.wait(), timeout=5)

        # Several lease windows elapse while the provider call is still parked.
        before = job_row(engine, claimed.job_id)["last_heartbeat_at"]
        clock.advance(45)
        await asyncio.sleep(0.2)
        assert job_row(engine, claimed.job_id)["last_heartbeat_at"] != before
        assert repository.inspect_claim(claimed, now=clock()) is ClaimState.ACTIVE

        release.set()
        outcome = await asyncio.wait_for(task, timeout=5)
        assert outcome is not None and outcome.status == "succeeded"
        assert blocking.calls == 1
        assert counts(engine)["run_events"] == 5
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_a_worker_without_the_configured_completion_strands_rather_than_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capability absence is a queue state: never fabricate an execution failure for it."""
    engine = prepare(tmp_path, monkeypatch)
    try:
        clock = MutableClock()
        completion = Completion()
        repository = SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None)
        claimed = claim(engine)
        orchestrator = JobExecutionService(
            repository,
            RunExecutor(create_builtin_handler_registry()),
            {},
            clock,
            sleep=no_sleep,
        )
        assert await orchestrator.execute(claimed) is None
        assert completion.calls == 0
        assert job_row(engine, claimed.job_id)["status"] == JobStatus.RUNNING.value
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT status FROM runs")) == "running"
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_the_run_snapshot_is_read_from_the_durable_row_not_mutable_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = prepare(tmp_path, monkeypatch)
    try:
        clock = MutableClock()
        completion = Completion()
        repository = SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None)
        claimed = claim(engine)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE agent_instances SET model_name='changed/model',"
                    " model_provider='openai' WHERE id=1"
                )
            )
        outcome = await service(engine, repository, completion, clock).execute(claimed)
        assert outcome is not None and outcome.status == "succeeded"
        assert completion.requests[0].model_name == "opaque/model"
    finally:
        engine.dispose()


def test_claim_dataclass_is_frozen_and_carries_the_fencing_values() -> None:
    claimed = ClaimedAttempt(1, 2, 3, 1, "worker", b"x" * 32, NOW)
    with pytest.raises(dataclasses.FrozenInstanceError):
        claimed.job_id = 9  # type: ignore[misc]
    # The token stays real in-memory authority of exactly the schema's 32 bytes.
    assert claimed.claim_token == b"x" * 32
    assert len(claimed.claim_token) == 32
    # ...but it is a bearer capability, so it is excluded from the generated repr.
    rendered = repr(claimed)
    assert "claim_token" not in rendered
    assert repr(b"x" * 32) not in rendered


@pytest.mark.anyio
async def test_the_claim_token_never_reaches_a_repr_a_log_or_a_run_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A claim token is real authority in memory and must never be rendered or recorded."""
    engine = prepare(tmp_path, monkeypatch)
    try:
        sentinel = "SENTINEL-CLAIM-TOKEN-LEAK-000000"
        assert len(sentinel) == 32

        def fixed_token(size: int) -> bytes:
            return sentinel.encode("ascii")[:size]

        monkeypatch.setattr(
            "nervos_core.infrastructure.database.jobs.os.urandom",
            fixed_token,
        )
        clock = MutableClock()
        repository = SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None)
        completion = Completion()
        with caplog.at_level(logging.DEBUG):
            claimed = claim(engine)
            assert len(claimed.claim_token) == 32
            assert claimed.claim_token == sentinel.encode("ascii")
            assert sentinel not in repr(claimed)
            assert "claim_token" not in repr(claimed)

            outcome = await service(engine, repository, completion, clock).execute(claimed)
            assert outcome is not None and outcome.status == "succeeded"
            assert completion.calls == 1

        # Nothing on the claim, start, heartbeat, or terminalization path logged the token.
        assert sentinel not in caplog.text
        with engine.connect() as connection:
            events = connection.execute(
                text("SELECT event_type, code, message FROM run_events"), ()
            ).all()
        assert len(events) == 5
        assert sentinel not in str(events)
        # The Job releases its claim on terminalization, as the frozen domain requires; the
        # Attempt keeps its 32-byte token because the domain requires every Attempt to carry
        # one. That retained value grants no authority: every fenced write needs the Job to be
        # `claimed`/`running`, so a terminal Attempt's token can never be used again.
        assert job_row(engine, claimed.job_id)["claim_token"] is None
        assert attempt_row(engine, claimed.attempt_id)["claim_token"] == sentinel.encode("ascii")
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_outer_cancellation_stops_the_local_task_and_writes_no_false_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shutdown must not detach a local provider task nor fabricate an outcome.

    Cancelling the orchestration task is the shutdown-grace case. NervOS stops waiting for
    and accepting a result; it claims no remote cancellation and no provider rollback, writes
    no terminal state, and leaves the claim for a later recovery milestone.
    """
    engine = prepare(tmp_path, monkeypatch)
    try:
        clock = MutableClock()
        entered = asyncio.Event()
        release = asyncio.Event()

        class Blocking(Completion):
            def __init__(self) -> None:
                super().__init__()
                self.cancelled = False
                self.finished = False

            async def complete(self, request: ModelRequest) -> ModelResponse:
                self.calls += 1
                self.requests.append(request)
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
                finally:
                    self.finished = True
                return ModelResponse(
                    "blocked answer",
                    PROVIDER,
                    request.model_name,
                    StopOutcome.STOP,
                    ModelUsage(11, 7, 18),
                )

        blocking = Blocking()
        repository = SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None)
        claimed = claim(engine)
        orchestrator = service(engine, repository, blocking, clock)
        task = asyncio.create_task(orchestrator.execute(claimed))
        await asyncio.wait_for(entered.wait(), timeout=5)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # `finished` proves the local provider task was cancelled *and collected*: had it been
        # left detached it would still be parked on `release` and could never have set it.
        assert blocking.cancelled is True
        assert blocking.finished is True
        assert blocking.calls == 1

        # No fabricated success, no fabricated failure, and no requeue.
        assert job_row(engine, claimed.job_id)["status"] == JobStatus.RUNNING.value
        assert attempt_row(engine, claimed.attempt_id)["status"] == AttemptStatus.RUNNING.value
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT status FROM runs")) == "running"
        assert event_types(engine, claimed.run_id) == [
            "run.created",
            "run.queued",
            "attempt.claimed",
            "attempt.started",
        ]
    finally:
        engine.dispose()
