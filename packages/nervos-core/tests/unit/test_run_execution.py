"""Provider-neutral RunExecutor parity tests, moved from the retired awaited coordinator.

Every scenario the Stage B coordinator covered is preserved here: the extraction moved the
logic verbatim, so these cases assert the same execution semantics against the new primitive.
The durable lifecycle scenarios that could not exist before C2 (Attempt, Job, Run Events,
fencing, lease authority) live in the JobExecutionService and terminalization suites.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from nervos_core.application.model_completion import (
    MODEL_OUTPUT_INCOMPLETE,
    MODEL_REFUSED,
    MODEL_RESPONSE_INVALID,
    MODEL_TIMED_OUT,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    StopOutcome,
)
from nervos_core.application.run_execution import (
    MAX_ELAPSED_MS,
    ExecutionOutcome,
    MonotonicClock,
    RunExecutor,
)
from nervos_core.application.trusted_chat import create_builtin_handler_registry
from nervos_core.domain.runs import ModelUsage, Run, RunLimits, RunStatus

NOW = datetime(2026, 1, 1, tzinfo=UTC)


class RecordingCompletion:
    """Deterministic provider double recording every request and blocking on demand."""

    def __init__(self, response: ModelResponse | None = None, error: Exception | None = None):
        self.requests: list[ModelRequest] = []
        self.started = asyncio.Event()
        self._release = asyncio.Event()
        self.response = response
        self.error = error
        self.block = False

    @property
    def calls(self) -> int:
        return len(self.requests)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        self.started.set()
        if self.block:
            await self._release.wait()
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response

    def release(self) -> None:
        self._release.set()


def ok_response(text: str = "answer", usage: ModelUsage | None = None) -> ModelResponse:
    return ModelResponse(text, "test-provider", "opaque/model", StopOutcome.STOP, usage)


def make_run(*, limits: RunLimits | None = None) -> Run:
    return Run(
        1,
        1,
        "nervos.chat",
        "1",
        "test-provider",
        "opaque/model",
        "prompt",
        limits or RunLimits(),
        RunStatus.RUNNING,
        NOW,
        NOW,
    )


def _zero_monotonic() -> int:
    return 0


def executor(monotonic: MonotonicClock = _zero_monotonic) -> RunExecutor:
    return RunExecutor(create_builtin_handler_registry(), monotonic)


@pytest.mark.anyio
async def test_success_returns_a_frozen_outcome_with_one_call() -> None:
    completion = RecordingCompletion(ok_response("hello there", ModelUsage(11, 7, None)))
    outcome = await executor().execute(make_run(), completion)
    assert outcome.status == "succeeded"
    assert outcome.output_text == "hello there"
    assert outcome.finish_reason == "stop"
    assert outcome.usage == ModelUsage(11, 7, None)
    assert outcome.error_code is None and outcome.error_message is None
    assert completion.calls == 1


@pytest.mark.anyio
async def test_multiline_output_is_preserved_exactly() -> None:
    completion = RecordingCompletion(ok_response("\tline\nsecond line\n"))
    outcome = await executor().execute(make_run(), completion)
    assert outcome.output_text == "\tline\nsecond line\n"


@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        (
            ModelResponse("x", "test-provider", "opaque/model", StopOutcome.INCOMPLETE),
            MODEL_OUTPUT_INCOMPLETE,
        ),
        (ModelResponse("x", "test-provider", "opaque/model", StopOutcome.REFUSED), MODEL_REFUSED),
        (
            ModelResponse("x", "test-provider", "opaque/model", StopOutcome.INVALID),
            MODEL_RESPONSE_INVALID,
        ),
        (
            ModelResponse("  \t\n", "test-provider", "opaque/model", StopOutcome.STOP),
            MODEL_RESPONSE_INVALID,
        ),
        (
            ModelResponse("x\x00", "test-provider", "opaque/model", StopOutcome.STOP),
            MODEL_RESPONSE_INVALID,
        ),
        (
            ModelResponse("x" * 40000, "test-provider", "opaque/model", StopOutcome.STOP),
            "model_output_too_large",
        ),
        (
            ModelResponse("x", "other-provider", "opaque/model", StopOutcome.STOP),
            MODEL_RESPONSE_INVALID,
        ),
    ],
)
@pytest.mark.anyio
async def test_execution_failures_become_safe_failed_outcomes(
    response: ModelResponse, expected_code: str
) -> None:
    completion = RecordingCompletion(response)
    outcome = await executor().execute(make_run(), completion)
    assert outcome.status == "failed"
    assert outcome.error_code == expected_code
    assert outcome.output_text is None
    assert outcome.finish_reason is None
    assert outcome.error_message
    assert completion.calls == 1


@pytest.mark.anyio
async def test_provider_error_becomes_a_failed_outcome() -> None:
    completion = RecordingCompletion(error=ModelProviderError(MODEL_RESPONSE_INVALID))
    outcome = await executor().execute(make_run(), completion)
    assert outcome.status == "failed"
    assert outcome.error_code == MODEL_RESPONSE_INVALID


@pytest.mark.anyio
async def test_timeout_becomes_a_failed_outcome_without_a_second_call() -> None:
    completion = RecordingCompletion(ok_response())
    completion.block = True
    outcome = await asyncio.wait_for(
        executor().execute(make_run(limits=RunLimits(provider_timeout_ms=50)), completion),
        timeout=10,
    )
    assert outcome.status == "failed"
    assert outcome.error_code == MODEL_TIMED_OUT
    assert outcome.output_text is None
    assert completion.calls == 1


@pytest.mark.anyio
async def test_external_cancellation_propagates_and_never_fakes_an_outcome() -> None:
    completion = RecordingCompletion(ok_response())
    completion.block = True
    task = asyncio.create_task(executor().execute(make_run(), completion))
    await asyncio.wait_for(completion.started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completion.calls == 1


def test_elapsed_time_is_clamped_and_never_negative() -> None:
    ticks = iter([0, -5, 500_000_000, MAX_ELAPSED_MS * 1_000_000 + 1])
    run_executor = executor(lambda: next(ticks))
    assert run_executor.elapsed_ms(0) == 0
    assert run_executor.elapsed_ms(0) == 0  # a negative interval clamps to zero
    assert run_executor.elapsed_ms(0) == 500  # nanoseconds become whole milliseconds
    assert run_executor.elapsed_ms(0) == MAX_ELAPSED_MS


def test_execution_outcome_rejects_an_inconsistent_shape() -> None:
    with pytest.raises(ValueError):
        ExecutionOutcome("succeeded", None, "stop", ModelUsage(), 1, None, None)
    with pytest.raises(ValueError):
        ExecutionOutcome("failed", "text", None, ModelUsage(), 1, "model_refused", "safe")
    with pytest.raises(ValueError):
        ExecutionOutcome("failed", None, None, ModelUsage(), 1, None, None)
