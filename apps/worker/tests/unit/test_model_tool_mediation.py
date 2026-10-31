# The Worker maps wire model tool traffic to and from the core ModelRequest.

from __future__ import annotations

from pathlib import Path

import pytest
from nervos_core.application.model_completion import (
    MODEL_REQUEST_REJECTED,
    AssistantTurn,
    ModelProviderError,
    ModelResponse,
    StopOutcome,
    ToolCall,
    ToolChoiceKind,
    ToolResultTurn,
    ToolSchema,
    UserTurn,
)
from nervos_core.application.model_completion import (
    ModelRequest as CoreModelRequest,
)
from nervos_core.domain.runs import ModelUsage
from nervos_worker.package_execution import PackageExecutionAdapter


class _FakeCompletion:
    def __init__(self, response: ModelResponse) -> None:
        self.response = response
        self.request: CoreModelRequest | None = None

    async def complete(self, request: CoreModelRequest) -> ModelResponse:
        self.request = request
        return self.response


def _frame(payload: dict[str, object]) -> dict[str, object]:
    return {
        "protocol_version": "1",
        "type": "model_request",
        "request_id": "port-1",
        "payload": payload,
    }


def _adapter() -> PackageExecutionAdapter:
    return PackageExecutionAdapter(Path("."))


@pytest.mark.anyio
async def test_tool_loop_roundtrip_maps_to_core_and_back() -> None:
    completion = _FakeCompletion(
        ModelResponse(
            text="",
            model_provider="fake",
            model_name="fake",
            finish_reason=StopOutcome.TOOL_USE,
            usage=ModelUsage(input_tokens=3, output_tokens=4, total_tokens=7),
            tool_calls=(ToolCall(call_id="call-1", name="echo", arguments_json='{"value": 1}'),),
        )
    )
    result = await _adapter().complete_model(
        completion,
        _frame(
            {
                "messages": [
                    {"role": "system", "content": "be brief"},
                    {"role": "user", "content": "hello"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {"id": "call-1", "name": "echo", "arguments": '{"value": 1}'}
                        ],
                    },
                    {"role": "tool", "content": "TOOL_OK", "tool_call_id": "call-1"},
                ],
                "model": "fake",
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo tool",
                        "parameters": {"type": "object", "properties": {}},
                    },
                    {
                        "name": "undeclared_tool",
                        "description": "Never declared",
                        "parameters": {"type": "object", "properties": {}},
                    },
                ],
            }
        ),
        "default-model",
        declared_tools=frozenset({"echo"}),
    )

    request = completion.request
    assert request is not None
    assert request.user_text == "hello"
    assert "be brief" in request.system_instruction
    # Only the declared tool is offered to the provider.
    assert request.tools == (
        ToolSchema(
            name="echo",
            description="Echo tool",
            input_schema={"type": "object", "properties": {}},
        ),
    )
    assert request.turns == (
        AssistantTurn(
            text="",
            tool_calls=(ToolCall(call_id="call-1", name="echo", arguments_json='{"value": 1}'),),
        ),
        ToolResultTurn(call_id="call-1", text="TOOL_OK"),
    )
    # The response frame carries the tool calls and the tool-use stop reason.
    assert result["stop_reason"] == "tool_use"
    assert result["tool_calls"] == [{"id": "call-1", "name": "echo", "arguments": '{"value": 1}'}]


@pytest.mark.anyio
async def test_tool_free_request_is_unchanged() -> None:
    completion = _FakeCompletion(ModelResponse(text="hi", model_provider="fake", model_name="fake"))
    result = await _adapter().complete_model(
        completion,
        _frame(
            {
                "messages": [
                    {"role": "system", "content": "be brief"},
                    {"role": "user", "content": "hello"},
                ],
                "model": "fake",
            }
        ),
        "default-model",
        declared_tools=frozenset({"echo"}),
    )

    request = completion.request
    assert request is not None
    assert request.tools == ()
    assert request.turns == ()
    assert request.user_text == "hello"
    assert "tool_calls" not in result
    assert result["stop_reason"] is None


@pytest.mark.anyio
async def test_tool_traffic_before_a_later_user_turn_now_succeeds_via_conversation() -> None:
    """ADR 0045 changed this from a refusal to the ordered-conversation mapping.

    (The pre-ADR test name was test_tool_traffic_before_the_current_user_turn_is_refused.)
    """
    completion = _FakeCompletion(ModelResponse(text="hi", model_provider="fake", model_name="fake"))
    result = await _adapter().complete_model(
        completion,
        _frame(
            {
                "messages": [
                    {"role": "user", "content": "first"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"id": "call-1", "name": "echo", "arguments": "{}"}],
                    },
                    {"role": "tool", "content": "ok", "tool_call_id": "call-1"},
                    {"role": "user", "content": "second"},
                ],
            }
        ),
        "default-model",
    )
    request = completion.request
    assert request is not None
    assert [type(item) for item in request.conversation] == [
        UserTurn,
        AssistantTurn,
        ToolResultTurn,
        UserTurn,
    ]
    assert request.history == ()
    assert request.turns == ()
    assert request.user_text == "second"
    assert result["output_text"] == "hi"


@pytest.mark.anyio
async def test_forced_tool_choice_maps_and_an_undeclared_named_choice_is_refused() -> None:
    completion = _FakeCompletion(ModelResponse(text="hi", model_provider="fake", model_name="fake"))
    await _adapter().complete_model(
        completion,
        _frame(
            {
                "messages": [{"role": "user", "content": "hello"}],
                "tool_choice": {"kind": "required"},
                "tools": [{"name": "echo", "description": "Echo", "parameters": {}}],
            }
        ),
        "default-model",
        declared_tools=frozenset({"echo"}),
    )
    request = completion.request
    assert request is not None
    assert request.tool_choice.kind is ToolChoiceKind.REQUIRED

    with pytest.raises(ModelProviderError) as error:
        await _adapter().complete_model(
            completion,
            _frame(
                {
                    "messages": [{"role": "user", "content": "hello"}],
                    "tool_choice": {"kind": "named", "name": "undeclared"},
                    "tools": [{"name": "echo", "description": "Echo", "parameters": {}}],
                }
            ),
            "default-model",
            declared_tools=frozenset({"echo"}),
        )
    assert error.value.code == MODEL_REQUEST_REJECTED


@pytest.mark.anyio
async def test_a_tool_message_must_reference_an_emitted_call() -> None:
    completion = _FakeCompletion(ModelResponse(text="hi", model_provider="fake", model_name="fake"))
    with pytest.raises(ModelProviderError) as error:
        await _adapter().complete_model(
            completion,
            _frame(
                {
                    "messages": [
                        {"role": "user", "content": "hello"},
                        {"role": "tool", "content": "orphan", "tool_call_id": "call-9"},
                    ],
                }
            ),
            "default-model",
        )
    assert error.value.code == MODEL_REQUEST_REJECTED


@pytest.mark.anyio
async def test_a_trailing_plain_assistant_stays_rejected() -> None:
    completion = _FakeCompletion(ModelResponse(text="hi", model_provider="fake", model_name="fake"))
    with pytest.raises(ModelProviderError) as error:
        await _adapter().complete_model(
            completion,
            _frame(
                {
                    "messages": [
                        {"role": "user", "content": "hello"},
                        {"role": "assistant", "content": "plain tail"},
                    ],
                }
            ),
            "default-model",
        )
    assert error.value.code == MODEL_REQUEST_REJECTED
