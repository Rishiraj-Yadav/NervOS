# ADR 0045: every adapter maps the neutral tool choice onto the vendor's own
# parameter (never emulated) and carries an ordered bridge conversation.

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from nervos_core.application.model_completion import (
    AssistantTurn,
    ModelProviderError,
    ModelRequest,
    ToolCall,
    ToolChoice,
    ToolChoiceKind,
    ToolResultTurn,
    ToolSchema,
    UserTurn,
)
from nervos_models.anthropic import AnthropicModelCompletion
from nervos_models.gemini import GeminiModelCompletion
from nervos_models.openai import OpenAIModelCompletion

TOOLS = (
    ToolSchema(name="echo", description="Echo", input_schema={"type": "object"}),
    ToolSchema(name="ping", description="Ping", input_schema={"type": "object"}),
)


def _request(**overrides: Any) -> ModelRequest:
    base: dict[str, Any] = {
        "system_instruction": "be brief",
        "user_text": "hello",
        "model_name": "opaque-model",
        "max_output_tokens": 99,
        "timeout_ms": 1500,
        "tools": TOOLS,
    }
    base.update(overrides)
    return ModelRequest(**base)


class _ResponsesCreate:
    """A fake ``responses.create`` returning one valid text item."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            status="completed",
            output=[
                SimpleNamespace(
                    type="message",
                    role="assistant",
                    content=[SimpleNamespace(type="output_text", text="ok")],
                )
            ],
            incomplete_details=None,
            error=None,
            usage=SimpleNamespace(input_tokens=2, output_tokens=3, total_tokens=5),
            model="opaque-model",
        )


class _MessagesCreate:
    """A fake ``messages.create`` returning one valid message."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            id="msg_1",
            type="message",
            role="assistant",
            content=[SimpleNamespace(type="text", text="ok")],
            model="opaque-model",
            stop_reason="end_turn",
            stop_sequence=None,
            usage=SimpleNamespace(input_tokens=2, output_tokens=3),
        )


class _GenerateContent:
    """A fake ``aio.models`` exposing ``generate_content``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, **kwargs: Any) -> Any:
        from google.genai import types

        self.calls.append(kwargs)
        return types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(role="model", parts=[types.Part(text="ok")]),
                    finish_reason=types.FinishReason.STOP,
                )
            ]
        )


def _openai_adapter(create: _ResponsesCreate) -> OpenAIModelCompletion:
    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    return OpenAIModelCompletion(cast("Any", client))


def _anthropic_adapter(create: _MessagesCreate) -> AnthropicModelCompletion:
    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    return AnthropicModelCompletion(cast("Any", client))


def _gemini_adapter(models: Any) -> GeminiModelCompletion:
    client = SimpleNamespace(aio=SimpleNamespace(models=models))
    return GeminiModelCompletion(cast("Any", client))


def _tool_choice_kwargs_of(call: dict[str, Any]) -> Any:
    return call.get("tool_choice")


@pytest.mark.anyio
async def test_openai_tool_choice_modes_map_onto_responses_kwargs() -> None:
    create = _ResponsesCreate()
    completion = _openai_adapter(create)

    # Default: byte-identical to the pre-ADR behavior.
    await completion.complete(_request())
    assert _tool_choice_kwargs_of(create.calls[-1]) == "auto"

    await completion.complete(_request(tool_choice=ToolChoice(ToolChoiceKind.NONE)))
    assert _tool_choice_kwargs_of(create.calls[-1]) == "none"

    await completion.complete(_request(tool_choice=ToolChoice(ToolChoiceKind.REQUIRED)))
    assert _tool_choice_kwargs_of(create.calls[-1]) == "required"

    await completion.complete(_request(tool_choice=ToolChoice(ToolChoiceKind.NAMED, name="echo")))
    assert _tool_choice_kwargs_of(create.calls[-1]) == {"type": "function", "name": "echo"}

    with pytest.raises(ModelProviderError):
        forced = ToolChoice(ToolChoiceKind.REQUIRED)
        await completion.complete(_request(tools=(), tool_choice=forced))
    with pytest.raises(ModelProviderError):
        await completion.complete(
            _request(tool_choice=ToolChoice(ToolChoiceKind.NAMED, name="missing"))
        )


@pytest.mark.anyio
async def test_openai_conversation_becomes_ordered_input_items() -> None:
    create = _ResponsesCreate()
    await _openai_adapter(create).complete(
        _request(
            conversation=(
                UserTurn(text="first"),
                AssistantTurn(
                    text="",
                    tool_calls=(
                        ToolCall(call_id="call-1", name="echo", arguments_json='{"value": 1}'),
                    ),
                ),
                ToolResultTurn(call_id="call-1", text="TOOL_RESULT_OK"),
                UserTurn(text="second"),
            )
        )
    )
    items = create.calls[-1]["input"]
    assert isinstance(items, list)
    assert items[0] == {"role": "user", "content": [{"type": "input_text", "text": "first"}]}
    function_call = cast("dict[str, Any]", items[1])
    assert function_call["type"] == "function_call"
    assert function_call["call_id"] == "call-1"
    output = cast("dict[str, Any]", items[2])
    assert output["type"] == "function_call_output"
    assert output["output"] == "TOOL_RESULT_OK"
    assert items[3] == {"role": "user", "content": [{"type": "input_text", "text": "second"}]}


@pytest.mark.anyio
async def test_anthropic_tool_choice_modes_map_onto_messages_kwargs() -> None:
    create = _MessagesCreate()
    completion = _anthropic_adapter(create)

    # Default: byte-identical to the pre-ADR behavior.
    await completion.complete(_request())
    assert _tool_choice_kwargs_of(create.calls[-1]) == {
        "type": "auto",
        "disable_parallel_tool_use": True,
    }

    await completion.complete(_request(tool_choice=ToolChoice(ToolChoiceKind.NONE)))
    assert _tool_choice_kwargs_of(create.calls[-1]) == {"type": "none"}

    await completion.complete(_request(tool_choice=ToolChoice(ToolChoiceKind.REQUIRED)))
    assert _tool_choice_kwargs_of(create.calls[-1]) == {
        "type": "any",
        "disable_parallel_tool_use": True,
    }

    await completion.complete(_request(tool_choice=ToolChoice(ToolChoiceKind.NAMED, name="ping")))
    assert _tool_choice_kwargs_of(create.calls[-1]) == {
        "type": "tool",
        "name": "ping",
        "disable_parallel_tool_use": True,
    }

    with pytest.raises(ModelProviderError):
        forced = ToolChoice(ToolChoiceKind.REQUIRED)
        await completion.complete(_request(tools=(), tool_choice=forced))


@pytest.mark.anyio
async def test_anthropic_conversation_becomes_ordered_messages() -> None:
    create = _MessagesCreate()
    await _anthropic_adapter(create).complete(
        _request(
            conversation=(
                UserTurn(text="first"),
                AssistantTurn(
                    text="",
                    tool_calls=(
                        ToolCall(call_id="call-1", name="echo", arguments_json='{"value": 1}'),
                    ),
                ),
                ToolResultTurn(call_id="call-1", text="TOOL_RESULT_OK"),
                UserTurn(text="second"),
            )
        )
    )
    messages = create.calls[-1]["messages"]
    assert messages[0] == {"role": "user", "content": "first"}
    assistant_blocks = messages[1]["content"]
    assert assistant_blocks[0]["type"] == "tool_use"
    assert assistant_blocks[0]["name"] == "echo"
    assert messages[2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "call-1",
                "content": [{"type": "text", "text": "TOOL_RESULT_OK"}],
            }
        ],
    }
    assert messages[3] == {"role": "user", "content": "second"}


@pytest.mark.anyio
async def test_gemini_tool_choice_modes_map_onto_function_calling_config() -> None:
    from google.genai import types

    models = _GenerateContent()
    completion = _gemini_adapter(models)

    # Default: byte-identical to the pre-ADR behavior.
    await completion.complete(_request())
    config = models.calls[-1]["config"]
    assert config.tool_config.function_calling_config.mode is types.FunctionCallingConfigMode.AUTO
    assert config.tool_config.function_calling_config.allowed_function_names is None

    await completion.complete(_request(tool_choice=ToolChoice(ToolChoiceKind.NONE)))
    config = models.calls[-1]["config"]
    assert config.tool_config.function_calling_config.mode is types.FunctionCallingConfigMode.NONE

    await completion.complete(_request(tool_choice=ToolChoice(ToolChoiceKind.REQUIRED)))
    config = models.calls[-1]["config"]
    assert config.tool_config.function_calling_config.mode is types.FunctionCallingConfigMode.ANY

    await completion.complete(_request(tool_choice=ToolChoice(ToolChoiceKind.NAMED, name="echo")))
    config = models.calls[-1]["config"]
    assert config.tool_config.function_calling_config.mode is types.FunctionCallingConfigMode.ANY
    assert config.tool_config.function_calling_config.allowed_function_names == ["echo"]

    with pytest.raises(ModelProviderError):
        forced = ToolChoice(ToolChoiceKind.REQUIRED)
        await completion.complete(_request(tools=(), tool_choice=forced))


@pytest.mark.anyio
async def test_gemini_conversation_becomes_ordered_contents() -> None:
    from google.genai import types

    class _Models:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def generate_content(self, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            return types.GenerateContentResponse(
                candidates=[
                    types.Candidate(
                        content=types.Content(role="model", parts=[types.Part(text="ok")]),
                        finish_reason=types.FinishReason.STOP,
                    )
                ]
            )

    models = _Models()
    client = SimpleNamespace(aio=SimpleNamespace(models=models))
    await GeminiModelCompletion(cast("Any", client)).complete(
        _request(
            conversation=(
                UserTurn(text="first"),
                AssistantTurn(
                    text="",
                    tool_calls=(
                        ToolCall(call_id="call-1", name="echo", arguments_json='{"value": 1}'),
                    ),
                ),
                ToolResultTurn(call_id="call-1", text="TOOL_RESULT_OK"),
                UserTurn(text="second"),
            )
        )
    )
    contents = models.calls[-1]["contents"]
    assert contents[0].role == "user"
    assert contents[0].parts[0].text == "first"
    assert contents[1].role == "model"
    assert contents[1].parts[0].function_call.name == "echo"
    assert contents[2].role == "user"
    assert contents[2].parts[0].function_response.name == "echo"
    assert contents[3].role == "user"
    assert contents[3].parts[0].text == "second"


@pytest.mark.anyio
async def test_default_requests_stay_byte_identical_without_tool_choice() -> None:
    # A request with tools but no explicit choice produces exactly the pre-ADR kwargs
    # in every adapter (tool_choice is the vendor's automatic form).
    create = _ResponsesCreate()
    await _openai_adapter(create).complete(_request())
    assert _tool_choice_kwargs_of(create.calls[-1]) == "auto"

    messages_create = _MessagesCreate()
    await _anthropic_adapter(messages_create).complete(_request())
    assert _tool_choice_kwargs_of(messages_create.calls[-1]) == {
        "type": "auto",
        "disable_parallel_tool_use": True,
    }


def test_a_named_choice_requires_a_name_and_only_a_named_choice_carries_one() -> None:
    with pytest.raises(ValueError):
        ToolChoice(ToolChoiceKind.NAMED)
    with pytest.raises(ValueError):
        ToolChoice(ToolChoiceKind.AUTO, name="echo")
    assert ToolChoice().kind is ToolChoiceKind.AUTO
