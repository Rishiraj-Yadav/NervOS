"""Cross-provider contract tests for the shared provider-neutral port.

Both production adapters must honor the same application-owned contract. These tests
exercise one attribute at a time rather than forcing two vendor shapes into one fake,
so a provider-specific regression stays visible instead of being averaged away.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import anthropic
import openai
import pytest
from nervos_core.application.model_completion import (
    ModelProviderError,
    ModelRequest,
    StopOutcome,
)
from nervos_models.anthropic import PROVIDER_ID as ANTHROPIC_ID
from nervos_models.anthropic import AnthropicModelCompletion
from nervos_models.openai import PROVIDER_ID as OPENAI_ID
from nervos_models.openai import OpenAIModelCompletion

REQUEST = ModelRequest("system text", "user text", "opaque/model", 1024, 1500)
SYNTHETIC_HIDDEN_REASONING = "SYNTHETIC-HIDDEN-REASONING-DO-NOT-LEAK"
SYNTHETIC_RAW_ERROR = "SYNTHETIC-RAW-PROVIDER-ERROR-DO-NOT-LEAK"


class _Recorder:
    """Record the exact request kwargs handed to one provider SDK call."""

    def __init__(self, response: Any = None, error: BaseException | None = None) -> None:
        self.kwargs: list[dict[str, Any]] = []
        self._response = response
        self._error = error

    async def __call__(self, **kwargs: Any) -> Any:
        self.kwargs.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response

    async def create(self, **kwargs: Any) -> Any:
        """Expose the SDK method name both vendors use for their one request."""
        return await self(**kwargs)


def _anthropic_adapter(recorder: _Recorder) -> AnthropicModelCompletion:
    return AnthropicModelCompletion(SimpleNamespace(messages=recorder))


def _openai_adapter(recorder: _Recorder) -> OpenAIModelCompletion:
    return OpenAIModelCompletion(SimpleNamespace(responses=recorder))


def _anthropic_text(value: str) -> Any:
    return SimpleNamespace(type="text", text=value)


def _anthropic_response(text: str, stop_reason: str = "end_turn") -> Any:
    return SimpleNamespace(
        content=[_anthropic_text(text)],
        model="claude-opus-5",
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=3, output_tokens=4),
    )


def _openai_response(text: str, status: str = "completed") -> Any:
    return SimpleNamespace(
        status=status,
        output=[
            SimpleNamespace(
                type="message",
                role="assistant",
                content=[SimpleNamespace(type="output_text", text=text)],
            )
        ],
        model="gpt-alias",
        error=None,
        incomplete_details=None,
        usage=SimpleNamespace(input_tokens=3, output_tokens=4, total_tokens=7),
    )


CASES = (
    (
        ANTHROPIC_ID,
        _anthropic_adapter,
        _anthropic_response,
        lambda: SimpleNamespace(code="overloaded_error", message=SYNTHETIC_RAW_ERROR),
        anthropic.APIConnectionError,
    ),
    (
        OPENAI_ID,
        _openai_adapter,
        _openai_response,
        lambda: SimpleNamespace(code="server_error", message=SYNTHETIC_RAW_ERROR),
        openai.APIConnectionError,
    ),
)


@pytest.mark.parametrize("provider_id,adapter,response,raw_error,connection_error", CASES)
@pytest.mark.anyio
async def test_both_providers_map_the_request_fields_identically(
    provider_id: str,
    adapter: Any,
    response: Any,
    raw_error: Any,
    connection_error: type[BaseException],
) -> None:
    recorder = _Recorder(response("answer"))

    result = await adapter(recorder).complete(REQUEST)

    assert result.model_provider == provider_id
    assert result.finish_reason is StopOutcome.STOP
    assert result.text == "answer"
    assert len(recorder.kwargs) == 1
    assert result.model_name.strip() != ""


@pytest.mark.parametrize("provider_id,adapter,response,raw_error,connection_error", CASES)
@pytest.mark.anyio
async def test_both_providers_make_exactly_one_call(
    provider_id: str,
    adapter: Any,
    response: Any,
    raw_error: Any,
    connection_error: type[BaseException],
) -> None:
    recorder = _Recorder(response("answer"))

    await adapter(recorder).complete(REQUEST)

    assert len(recorder.kwargs) == 1, provider_id


@pytest.mark.parametrize("provider_id,adapter,response,raw_error,connection_error", CASES)
@pytest.mark.anyio
async def test_both_providers_send_the_opaque_run_model_unchanged(
    provider_id: str,
    adapter: Any,
    response: Any,
    raw_error: Any,
    connection_error: type[BaseException],
) -> None:
    """The immutable Run snapshot owns model identity; a provider alias is not echoed into it."""
    recorder = _Recorder(response("answer"))

    await adapter(recorder).complete(REQUEST)

    assert recorder.kwargs[0]["model"] == "opaque/model"


@pytest.mark.parametrize("provider_id,adapter,response,raw_error,connection_error", CASES)
@pytest.mark.anyio
async def test_both_providers_normalize_provider_unavailability_safely(
    provider_id: str,
    adapter: Any,
    response: Any,
    raw_error: Any,
    connection_error: type[BaseException],
) -> None:
    error = connection_error.__new__(connection_error)
    recorder = _Recorder(error=error)

    with pytest.raises(ModelProviderError) as caught:
        await adapter(recorder).complete(REQUEST)

    assert caught.value.code == "model_unavailable"
    assert SYNTHETIC_RAW_ERROR not in caught.value.message


@pytest.mark.parametrize("provider_id,adapter,response,raw_error,connection_error", CASES)
@pytest.mark.anyio
async def test_both_providers_let_external_cancellation_propagate(
    provider_id: str,
    adapter: Any,
    response: Any,
    raw_error: Any,
    connection_error: type[BaseException],
) -> None:
    recorder = _Recorder(error=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await adapter(recorder).complete(REQUEST)

    assert len(recorder.kwargs) == 1


@pytest.mark.parametrize("provider_id,adapter,response,raw_error,connection_error", CASES)
@pytest.mark.anyio
async def test_both_providers_reject_unknown_stop_outcomes(
    provider_id: str,
    adapter: Any,
    response: Any,
    raw_error: Any,
    connection_error: type[BaseException],
) -> None:
    """Neither adapter may treat an unrecognized provider outcome as success."""
    if provider_id == ANTHROPIC_ID:
        payload = _anthropic_response("answer", stop_reason="pause_turn")
    else:
        payload = _openai_response("answer", status="in_progress")

    with pytest.raises(ModelProviderError) as caught:
        await adapter(_Recorder(payload)).complete(REQUEST)

    assert caught.value.code == "model_response_invalid"
