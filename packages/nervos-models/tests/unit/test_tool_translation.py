"""D4 tool translation for both provider adapters.

Each adapter's job here is narrow and symmetrical: report that the model asked for a tool in the one
canonical neutral form, and render a continuation back into the provider's own wire shape without
losing anything the tool produced. These tests pin both directions, and they pin the tool-free wire
path staying exactly as strict as it was before the tool layer existed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from nervos_core.application.model_completion import (
    MODEL_RESPONSE_INVALID,
    AssistantTurn,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    StopOutcome,
    ToolCall,
    ToolResultTurn,
    ToolSchema,
)
from nervos_models.anthropic import AnthropicModelCompletion
from nervos_models.openai import OpenAIModelCompletion

SCHEMA = ToolSchema(
    name="nervos__builtin__current_time_abc123def456",
    description="Report the current time.",
    input_schema={"type": "object", "properties": {"timezone": {"type": "string"}}},
)
CALL = ToolCall(
    call_id="call_1",
    name="nervos__builtin__current_time_abc123def456",
    arguments_json='{"timezone":"UTC"}',
)


class _Recorder:
    """A provider client double that records the exact kwargs of each call."""

    def __init__(self, response: Any) -> None:
        self.kwargs: list[dict[str, Any]] = []
        self._response = response

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs.append(kwargs)
        return self._response


class _Client:
    def __init__(self, resource: str, recorder: _Recorder) -> None:
        setattr(self, resource, recorder)


def _anthropic(response: Any) -> tuple[AnthropicModelCompletion, _Recorder]:
    recorder = _Recorder(response)
    return AnthropicModelCompletion(_Client("messages", recorder)), recorder


def _openai(response: Any) -> tuple[OpenAIModelCompletion, _Recorder]:
    recorder = _Recorder(response)
    return OpenAIModelCompletion(_Client("responses", recorder)), recorder


def _anthropic_response(*blocks: Any, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        content=list(blocks), model="claude-opus-5", stop_reason=stop_reason, usage=None
    )


def _openai_response(*items: Any, status: str = "completed") -> SimpleNamespace:
    return SimpleNamespace(output=list(items), status=status, error=None, usage=None)


def _text_block(value: str) -> Any:
    return SimpleNamespace(type="text", text=value)


def _tool_use_block(call_id: str, name: str, payload: dict[str, Any]) -> Any:
    return SimpleNamespace(type="tool_use", id=call_id, name=name, input=payload)


def _request(**overrides: Any) -> ModelRequest:
    base: dict[str, Any] = {
        "system_instruction": "sys",
        "user_text": "usr",
        "model_name": "opaque/model",
        "max_output_tokens": 1024,
        "timeout_ms": 1500,
    }
    base.update(overrides)
    return ModelRequest(**base)


class TestAnthropicToolDeclaration:
    @pytest.mark.anyio
    async def test_a_tool_free_request_declares_no_tools_and_no_choice(self) -> None:
        completion, recorder = _anthropic(_anthropic_response(_text_block("hi")))

        await completion.complete(_request())

        assert "tools" not in recorder.kwargs[0]
        assert "tool_choice" not in recorder.kwargs[0]
        # The tool-free wire shape is unchanged from Stage B/C.
        assert recorder.kwargs[0]["messages"] == [{"role": "user", "content": "usr"}]

    @pytest.mark.anyio
    async def test_tools_are_declared_with_parallel_use_disabled(self) -> None:
        completion, recorder = _anthropic(_anthropic_response(_text_block("hi")))

        await completion.complete(_request(tools=(SCHEMA,)))

        assert recorder.kwargs[0]["tools"] == [
            {
                "name": SCHEMA.name,
                "description": SCHEMA.description,
                "input_schema": SCHEMA.input_schema,
            }
        ]
        assert recorder.kwargs[0]["tool_choice"] == {
            "type": "auto",
            "disable_parallel_tool_use": True,
        }


class TestAnthropicToolCalls:
    @pytest.mark.anyio
    async def test_a_tool_use_block_becomes_one_canonical_tool_call(self) -> None:
        completion, _ = _anthropic(
            _anthropic_response(
                _tool_use_block("call_1", SCHEMA.name, {"timezone": "UTC"}),
                stop_reason="tool_use",
            )
        )

        response = await completion.complete(_request(tools=(SCHEMA,)))

        assert response.finish_reason is StopOutcome.TOOL_USE
        assert response.tool_calls == (
            ToolCall(
                call_id="call_1",
                name=SCHEMA.name,
                arguments_json='{"timezone":"UTC"}',
            ),
        )

    @pytest.mark.anyio
    async def test_arguments_are_normalized_to_canonical_json_text(self) -> None:
        completion, _ = _anthropic(
            _anthropic_response(
                _tool_use_block("call_1", SCHEMA.name, {"b": 1, "a": 2}), stop_reason="tool_use"
            )
        )

        response = await completion.complete(_request(tools=(SCHEMA,)))

        # Key order is normalized here so the payload is stable regardless of provider ordering.
        assert response.tool_calls[0].arguments_json == '{"a":2,"b":1}'

    @pytest.mark.anyio
    async def test_prose_beside_a_tool_use_survives(self) -> None:
        completion, _ = _anthropic(
            _anthropic_response(
                _text_block("let me check"),
                _tool_use_block("call_1", SCHEMA.name, {}),
                stop_reason="tool_use",
            )
        )

        response = await completion.complete(_request(tools=(SCHEMA,)))

        assert response.text == "let me check"
        assert len(response.tool_calls) == 1

    @pytest.mark.anyio
    async def test_a_tool_turn_with_no_text_blocks_is_accepted(self) -> None:
        completion, _ = _anthropic(
            _anthropic_response(_tool_use_block("call_1", SCHEMA.name, {}), stop_reason="tool_use")
        )

        response = await completion.complete(_request(tools=(SCHEMA,)))

        assert response.text == ""
        assert len(response.tool_calls) == 1

    @pytest.mark.anyio
    async def test_a_tool_use_block_without_offered_tools_still_fails_closed(self) -> None:
        completion, _ = _anthropic(
            _anthropic_response(_tool_use_block("call_1", SCHEMA.name, {}), stop_reason="tool_use")
        )

        with pytest.raises(ModelProviderError) as raised:
            await completion.complete(_request())

        assert raised.value.code == MODEL_RESPONSE_INVALID

    @pytest.mark.anyio
    async def test_a_tool_stop_without_offered_tools_still_fails_closed(self) -> None:
        completion, _ = _anthropic(_anthropic_response(_text_block("hi"), stop_reason="tool_use"))

        with pytest.raises(ModelProviderError) as raised:
            await completion.complete(_request())

        assert raised.value.code == MODEL_RESPONSE_INVALID

    @pytest.mark.anyio
    async def test_a_hidden_reasoning_block_is_still_never_exposed(self) -> None:
        completion, _ = _anthropic(
            _anthropic_response(
                SimpleNamespace(type="thinking", thinking="secret"),
                _text_block("visible"),
            )
        )

        response = await completion.complete(_request(tools=(SCHEMA,)))

        assert response.text == "visible"
        assert "secret" not in repr(response)


class TestAnthropicContinuation:
    @pytest.mark.anyio
    async def test_an_assistant_turn_and_its_result_are_rendered_in_order(self) -> None:
        completion, recorder = _anthropic(_anthropic_response(_text_block("done")))
        request = _request(
            tools=(SCHEMA,),
            turns=(
                AssistantTurn(text="let me check", tool_calls=(CALL,)),
                ToolResultTurn(call_id=CALL.call_id, text="12:00", structured={"hour": 12}),
            ),
        )

        await completion.complete(request)

        messages = recorder.kwargs[0]["messages"]
        assert messages[0] == {"role": "user", "content": "usr"}
        assert messages[1]["role"] == "assistant"
        assert messages[1]["content"][0] == {"type": "text", "text": "let me check"}
        assert messages[1]["content"][1] == {
            "type": "tool_use",
            "id": CALL.call_id,
            "name": CALL.name,
            "input": {"timezone": "UTC"},
        }
        # The result keeps both channels: its text and its canonical structured JSON.
        assert messages[2] == {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": CALL.call_id,
                    "content": [
                        {"type": "text", "text": "12:00"},
                        {"type": "text", "text": '{"hour":12}'},
                    ],
                }
            ],
        }

    @pytest.mark.anyio
    async def test_each_rounds_results_land_before_the_next_assistant_turn(self) -> None:
        """A multi-round conversation must keep its rounds in order.

        The second round's assistant turn may not be emitted before the first round's results, or a
        later result would be answered against the wrong turn.
        """
        completion, recorder = _anthropic(_anthropic_response(_text_block("done")))
        second = ToolCall(call_id="call_2", name=CALL.name, arguments_json='{"timezone":"UTC"}')
        request = _request(
            tools=(SCHEMA,),
            turns=(
                AssistantTurn(text="", tool_calls=(CALL,)),
                ToolResultTurn(call_id=CALL.call_id, text="first"),
                AssistantTurn(text="", tool_calls=(second,)),
                ToolResultTurn(call_id=second.call_id, text="second"),
            ),
        )

        await completion.complete(request)

        messages = recorder.kwargs[0]["messages"]
        assert [message["role"] for message in messages] == [
            "user",
            "assistant",
            "user",
            "assistant",
            "user",
        ]
        assert messages[2]["content"][0]["tool_use_id"] == CALL.call_id
        assert messages[4]["content"][0]["tool_use_id"] == second.call_id

    @pytest.mark.anyio
    async def test_an_assistant_turn_without_prose_omits_the_text_block(self) -> None:
        completion, recorder = _anthropic(_anthropic_response(_text_block("done")))
        request = _request(
            tools=(SCHEMA,),
            turns=(
                AssistantTurn(text="", tool_calls=(CALL,)),
                ToolResultTurn(call_id=CALL.call_id, text="ok"),
            ),
        )

        await completion.complete(request)

        assert recorder.kwargs[0]["messages"][1]["content"] == [
            {
                "type": "tool_use",
                "id": CALL.call_id,
                "name": CALL.name,
                "input": {"timezone": "UTC"},
            }
        ]

    @pytest.mark.anyio
    async def test_an_error_result_is_marked_as_such(self) -> None:
        completion, recorder = _anthropic(_anthropic_response(_text_block("done")))
        request = _request(
            tools=(SCHEMA,),
            turns=(
                AssistantTurn(text="", tool_calls=(CALL,)),
                ToolResultTurn(call_id=CALL.call_id, text="Denied.", is_error=True),
            ),
        )

        await completion.complete(request)

        block = recorder.kwargs[0]["messages"][2]["content"][0]
        assert block["is_error"] is True


def _function_call(call_id: str, name: str, arguments: str) -> Any:
    return SimpleNamespace(type="function_call", call_id=call_id, name=name, arguments=arguments)


def _message(text: str) -> Any:
    return SimpleNamespace(
        type="message",
        role="assistant",
        content=[SimpleNamespace(type="output_text", text=text)],
    )


class TestOpenAIToolDeclaration:
    @pytest.mark.anyio
    async def test_a_tool_free_request_keeps_the_string_input(self) -> None:
        completion, recorder = _openai(_openai_response(_message("hi")))

        await completion.complete(_request())

        assert "tools" not in recorder.kwargs[0]
        assert "tool_choice" not in recorder.kwargs[0]
        assert recorder.kwargs[0]["input"] == "usr"
        assert recorder.kwargs[0]["store"] is False

    @pytest.mark.anyio
    async def test_local_functions_are_declared_with_automatic_choice(self) -> None:
        completion, recorder = _openai(_openai_response(_message("hi")))

        await completion.complete(_request(tools=(SCHEMA,)))

        assert recorder.kwargs[0]["tools"] == [
            {
                "type": "function",
                "name": SCHEMA.name,
                "description": SCHEMA.description,
                "parameters": SCHEMA.input_schema,
                "strict": False,
            }
        ]
        assert recorder.kwargs[0]["tool_choice"] == "auto"


class TestOpenAIToolCalls:
    @pytest.mark.anyio
    async def test_a_function_call_item_becomes_one_canonical_tool_call(self) -> None:
        completion, _ = _openai(
            _openai_response(_function_call("call_1", SCHEMA.name, '{"timezone":"UTC"}'))
        )

        response = await completion.complete(_request(tools=(SCHEMA,)))

        assert response.tool_calls == (
            ToolCall(call_id="call_1", name=SCHEMA.name, arguments_json='{"timezone":"UTC"}'),
        )

    @pytest.mark.anyio
    async def test_both_adapters_produce_the_identical_neutral_call(self) -> None:
        """One canonical form is what keeps a provider-shaped object from escaping."""
        anthropic, _ = _anthropic(
            _anthropic_response(
                _tool_use_block("call_1", SCHEMA.name, {"timezone": "UTC"}),
                stop_reason="tool_use",
            )
        )
        openai, _ = _openai(
            _openai_response(_function_call("call_1", SCHEMA.name, '{"timezone":"UTC"}'))
        )

        from_anthropic = await anthropic.complete(_request(tools=(SCHEMA,)))
        from_openai = await openai.complete(_request(tools=(SCHEMA,)))

        assert from_anthropic.tool_calls == from_openai.tool_calls
        assert isinstance(from_anthropic.tool_calls[0].arguments_json, str)

    @pytest.mark.anyio
    async def test_prose_beside_a_tool_call_survives(self) -> None:
        completion, _ = _openai(
            _openai_response(_message("let me check"), _function_call("call_1", SCHEMA.name, "{}"))
        )

        response = await completion.complete(_request(tools=(SCHEMA,)))

        assert response.text == "let me check"
        assert len(response.tool_calls) == 1

    @pytest.mark.anyio
    async def test_a_function_call_without_offered_tools_still_fails_closed(self) -> None:
        completion, _ = _openai(_openai_response(_function_call("call_1", SCHEMA.name, "{}")))

        with pytest.raises(ModelProviderError) as raised:
            await completion.complete(_request())

        assert raised.value.code == MODEL_RESPONSE_INVALID

    @pytest.mark.anyio
    async def test_a_provider_hosted_tool_item_is_still_rejected(self) -> None:
        """An item the provider executed itself is never accepted, tools offered or not."""
        for item_type in ("shell_call", "computer_call", "mcp_call", "code_interpreter_call"):
            completion, _ = _openai(
                _openai_response(SimpleNamespace(type=item_type), _message("hi"))
            )
            with pytest.raises(ModelProviderError) as raised:
                await completion.complete(_request(tools=(SCHEMA,)))
            assert raised.value.code == MODEL_RESPONSE_INVALID, item_type


class TestOpenAIContinuation:
    @pytest.mark.anyio
    async def test_history_is_rendered_as_items_with_results_addressed_back(self) -> None:
        completion, recorder = _openai(_openai_response(_message("done")))
        request = _request(
            tools=(SCHEMA,),
            turns=(
                AssistantTurn(text="let me check", tool_calls=(CALL,)),
                ToolResultTurn(call_id=CALL.call_id, text="12:00", structured={"hour": 12}),
            ),
        )

        await completion.complete(request)

        items = recorder.kwargs[0]["input"]
        assert items[0] == {
            "role": "user",
            "content": [{"type": "input_text", "text": "usr"}],
        }
        assert items[1] == {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "let me check"}],
        }
        assert items[2] == {
            "type": "function_call",
            "call_id": CALL.call_id,
            "name": CALL.name,
            "arguments": '{"timezone":"UTC"}',
        }
        # The wire field is a string, so the structured member is appended rather than dropped.
        assert items[3] == {
            "type": "function_call_output",
            "call_id": CALL.call_id,
            "output": '12:00\n{"hour":12}',
        }

    @pytest.mark.anyio
    async def test_a_text_only_result_is_carried_verbatim(self) -> None:
        completion, recorder = _openai(_openai_response(_message("done")))
        request = _request(
            tools=(SCHEMA,),
            turns=(
                AssistantTurn(text="", tool_calls=(CALL,)),
                ToolResultTurn(call_id=CALL.call_id, text="ok"),
            ),
        )

        await completion.complete(request)

        items = recorder.kwargs[0]["input"]
        assert items[1] == {
            "type": "function_call",
            "call_id": CALL.call_id,
            "name": CALL.name,
            "arguments": '{"timezone":"UTC"}',
        }
        assert items[2]["output"] == "ok"


class TestToolFreeCompatibility:
    """The tool-free paths of both adapters must behave exactly as they did before D4."""

    @pytest.mark.anyio
    async def test_anthropic_still_reports_a_plain_stop(self) -> None:
        completion, _ = _anthropic(
            _anthropic_response(_text_block("hello"), stop_reason="end_turn")
        )

        response = await completion.complete(_request())

        assert response.finish_reason is StopOutcome.STOP
        assert response.tool_calls == ()
        assert response.text == "hello"

    @pytest.mark.anyio
    async def test_openai_still_requires_visible_text_for_a_completed_response(self) -> None:
        completion, _ = _openai(_openai_response(_message("   ")))

        with pytest.raises(ModelProviderError) as raised:
            await completion.complete(_request())

        assert raised.value.code == MODEL_RESPONSE_INVALID

    @pytest.mark.anyio
    async def test_a_neutral_response_carries_no_provider_object(self) -> None:
        completion, _ = _openai(_openai_response(_message("hello")))

        response = await completion.complete(_request(tools=(SCHEMA,)))

        assert isinstance(response, ModelResponse)
        assert isinstance(response.text, str)
        # A provider that reported no usage is reported as absent, never invented as zeros.
        assert response.usage is None
        assert isinstance(response.tool_calls, tuple)
