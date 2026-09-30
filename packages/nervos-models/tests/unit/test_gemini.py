"""Deterministic contract tests for the Google Gemini Developer API adapter."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import google.genai.errors as genai_errors
import httpx
import pytest
from google.genai import types
from nervos_core.application.model_completion import (
    AssistantTurn,
    ModelProviderError,
    ModelRequest,
    StopOutcome,
    ToolCall,
    ToolResultTurn,
    ToolSchema,
)
from nervos_models.gemini import GeminiModelCompletion

REQUEST = ModelRequest("system text", "user text", "opaque/gemini-model", 88, 1250)
SCHEMA = ToolSchema(
    "nervos__builtin__current_time_abc123def456",
    "Report current time.",
    {
        "type": "object",
        "properties": {"timezone": {"type": "string"}},
        "required": ["timezone"],
        "additionalProperties": False,
    },
)


class _Generate:
    def __init__(self, response: Any = None, error: BaseException | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def _response(*parts: types.Part, finish_reason: Any = "STOP", **overrides: Any) -> Any:
    candidate = types.Candidate(
        content=types.Content(role="model", parts=list(parts)), finish_reason=finish_reason
    )
    return types.GenerateContentResponse(
        candidates=[candidate],
        response_id=overrides.get("response_id", "response-123"),
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=3, candidates_token_count=4, total_token_count=9
        ),
    )


def _adapter(generate: _Generate) -> GeminiModelCompletion:
    return GeminiModelCompletion(SimpleNamespace(aio=SimpleNamespace(models=generate)))  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_tool_free_request_uses_explicit_system_and_normalizes_usage() -> None:
    generate = _Generate(_response(types.Part(text="hello")))

    result = await _adapter(generate).complete(REQUEST)

    assert result.text == "hello"
    assert result.model_provider == "gemini"
    assert result.model_name == REQUEST.model_name
    assert result.finish_reason is StopOutcome.STOP
    assert result.usage is not None and result.usage.values() == (3, 4, 9)
    sent = generate.calls[0]
    assert sent["model"] == REQUEST.model_name
    assert [part.text for part in sent["contents"][-1].parts] == ["user text"]
    assert sent["config"].system_instruction == "system text"
    assert sent["config"].max_output_tokens == 88
    assert sent["config"].http_options.timeout == 1250
    assert sent["config"].http_options.retry_options.attempts == 1
    assert sent["config"].automatic_function_calling.disable is True


@pytest.mark.anyio
async def test_tool_schema_is_manual_and_function_calls_are_normalized() -> None:
    function = types.FunctionCall(id="google-call-id", name=SCHEMA.name, args={"timezone": "UTC"})
    generate = _Generate(_response(types.Part(function_call=function), finish_reason="STOP"))

    result = await _adapter(generate).complete(
        ModelRequest("sys", "user", "model", 50, 1000, tools=(SCHEMA,))
    )

    assert result.finish_reason is StopOutcome.TOOL_USE
    assert result.tool_calls == (ToolCall("google-call-id", SCHEMA.name, '{"timezone":"UTC"}'),)
    declaration = generate.calls[0]["config"].tools[0].function_declarations[0]
    assert declaration.name == SCHEMA.name
    assert declaration.parameters_json_schema == SCHEMA.input_schema
    assert generate.calls[0]["config"].tool_config.function_calling_config.mode == "AUTO"


@pytest.mark.anyio
async def test_function_response_roundtrip_preserves_google_id_and_signature() -> None:
    signature = b"opaque-signature"
    generate = _Generate(
        _response(
            types.Part(
                function_call=types.FunctionCall(id="google-call-id", name=SCHEMA.name, args={}),
                thought=True,
                thought_signature=signature,
            )
        )
    )
    adapter = _adapter(generate)
    first = await adapter.complete(ModelRequest("sys", "user", "model", 50, 1000, tools=(SCHEMA,)))
    call_id = first.tool_calls[0].call_id
    generate.response = _response(types.Part(text="done"))

    await adapter.complete(
        ModelRequest(
            "sys",
            "user",
            "model",
            50,
            1000,
            tools=(SCHEMA,),
            turns=(
                AssistantTurn("", first.tool_calls),
                ToolResultTurn(call_id, "42", structured={"result": 42}),
            ),
        )
    )

    history = generate.calls[1]["contents"]
    sent_call = history[-2].parts[0]
    sent_result = history[-1].parts[0].function_response
    assert sent_call.function_call.id == "google-call-id"
    assert sent_call.thought_signature == signature
    assert sent_result.id == "google-call-id"
    assert sent_result.name == SCHEMA.name
    assert sent_result.response == {"result": 42}


@pytest.mark.anyio
async def test_thought_part_stays_adapter_private_but_survives_tool_roundtrip() -> None:
    generate = _Generate(
        _response(
            types.Part(text="private reasoning", thought=True, thought_signature=b"thought-sig"),
            types.Part(function_call=types.FunctionCall(id="call-1", name=SCHEMA.name, args={})),
        )
    )
    adapter = _adapter(generate)
    first = await adapter.complete(ModelRequest("sys", "user", "model", 50, 1000, tools=(SCHEMA,)))
    assert "private reasoning" not in first.text
    generate.response = _response(types.Part(text="done"))
    await adapter.complete(
        ModelRequest(
            "sys",
            "user",
            "model",
            50,
            1000,
            tools=(SCHEMA,),
            turns=(
                AssistantTurn("", first.tool_calls),
                ToolResultTurn("call-1", "done"),
            ),
        )
    )
    thought = generate.calls[1]["contents"][-2].parts[0]
    assert thought.text == "private reasoning"
    assert thought.thought is True
    assert thought.thought_signature == b"thought-sig"


@pytest.mark.anyio
async def test_multiple_tool_calls_preserve_order_and_reject_duplicate_ids() -> None:
    parts = (
        types.Part(function_call=types.FunctionCall(id="first", name=SCHEMA.name, args={})),
        types.Part(function_call=types.FunctionCall(id="second", name=SCHEMA.name, args={})),
    )
    response = await _adapter(_Generate(_response(*parts))).complete(
        ModelRequest("sys", "user", "model", 50, 1000, tools=(SCHEMA,))
    )
    assert [call.call_id for call in response.tool_calls] == ["first", "second"]
    duplicate = (parts[0], parts[0])
    with pytest.raises(ModelProviderError) as raised:
        await _adapter(_Generate(_response(*duplicate))).complete(
            ModelRequest("sys", "user", "model", 50, 1000, tools=(SCHEMA,))
        )
    assert raised.value.code == "model_response_invalid"


@pytest.mark.anyio
async def test_unsupported_data_is_rejected_even_alongside_text() -> None:
    response = _response(
        types.Part(text="answer", inline_data=types.Blob(data=b"x", mime_type="image/png"))
    )
    with pytest.raises(ModelProviderError) as raised:
        await _adapter(_Generate(response)).complete(REQUEST)
    assert raised.value.code == "model_response_invalid"


@pytest.mark.parametrize(
    ("reason", "outcome"),
    [
        ("MAX_TOKENS", StopOutcome.INCOMPLETE),
    ],
)
@pytest.mark.anyio
async def test_finish_reasons_normalize_without_partial_output(
    reason: str, outcome: StopOutcome
) -> None:
    generate = _Generate(_response(types.Part(text="partial secret"), finish_reason=reason))

    result = await _adapter(generate).complete(REQUEST)

    assert result.finish_reason is outcome
    assert result.text == ""


@pytest.mark.parametrize("reason", ["SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT"])
@pytest.mark.anyio
async def test_safety_reasons_refuse_without_exposing_partial_output(reason: str) -> None:
    with pytest.raises(ModelProviderError) as raised:
        await _adapter(
            _Generate(_response(types.Part(text="blocked"), finish_reason=reason))
        ).complete(REQUEST)
    assert raised.value.code == "model_refused"
    assert "blocked" not in str(raised.value)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (genai_errors.ClientError(401, {}), "model_authentication_failed"),
        (genai_errors.ClientError(403, {}), "model_permission_denied"),
        (genai_errors.ClientError(404, {}), "model_request_rejected"),
        (genai_errors.ClientError(429, {}), "model_rate_limited"),
        (genai_errors.ServerError(503, {}), "model_unavailable"),
        (TimeoutError("secret"), "model_timed_out"),
        (httpx.ConnectError("secret endpoint"), "model_unavailable"),
    ],
)
@pytest.mark.anyio
async def test_provider_errors_are_normalized_safely(error: BaseException, code: str) -> None:
    with pytest.raises(ModelProviderError) as raised:
        await _adapter(_Generate(error=error)).complete(REQUEST)
    assert raised.value.code == code
    assert "secret" not in str(raised.value).lower()


@pytest.mark.parametrize(
    "response",
    [None, SimpleNamespace(candidates=[]), _response(types.Part(text=""), finish_reason="FUTURE")],
)
@pytest.mark.anyio
async def test_missing_empty_and_unknown_responses_fail_closed(response: Any) -> None:
    with pytest.raises(ModelProviderError) as raised:
        await _adapter(_Generate(response)).complete(REQUEST)
    assert raised.value.code == "model_response_invalid"


@pytest.mark.anyio
async def test_a_function_call_is_rejected_when_no_function_was_offered() -> None:
    response = _response(types.Part(function_call=types.FunctionCall(name="not-offered", args={})))
    with pytest.raises(ModelProviderError) as raised:
        await _adapter(_Generate(response)).complete(REQUEST)
    assert raised.value.code == "model_response_invalid"


@pytest.mark.anyio
async def test_external_cancellation_propagates() -> None:
    with pytest.raises(asyncio.CancelledError):
        await _adapter(_Generate(error=asyncio.CancelledError())).complete(REQUEST)
