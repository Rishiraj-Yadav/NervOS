# ADR 0042: the wire carries model tool traffic additively; old frames stay valid.

from __future__ import annotations

import io
from typing import cast

from nervos_package_host.wire import (
    HOST_CAPABILITIES,
    HOST_PROTOCOL_VERSION,
    MODEL_TOOLS_HOST_CAPABILITY,
    read_frame,
    write_frame,
)


def _roundtrip(message: dict[str, object]) -> dict[str, object]:
    stream = io.BytesIO()
    write_frame(stream, message)
    stream.seek(0)
    return read_frame(stream)


def test_model_tools_capability_is_advertised() -> None:
    assert MODEL_TOOLS_HOST_CAPABILITY in HOST_CAPABILITIES


def test_model_request_frame_with_tools_and_tool_messages_roundtrips() -> None:
    frame = _roundtrip(
        {
            "protocol_version": HOST_PROTOCOL_VERSION,
            "type": "model_request",
            "request_id": "port-1",
            "payload": {
                "messages": [
                    {"role": "system", "content": "be brief"},
                    {"role": "user", "content": "hello"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "name": "echo",
                                "arguments": '{"value": 1}',
                            }
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
                    }
                ],
            },
        }
    )
    payload = frame["payload"]
    assert isinstance(payload, dict)
    payload_item = cast("dict[str, object]", payload)
    assert isinstance(payload_item["tools"], list)
    messages = payload_item["messages"]
    assert isinstance(messages, list)
    message_items = cast("list[object]", messages)
    assistant = message_items[2]
    assert isinstance(assistant, dict)
    tool_calls = cast("dict[str, object]", assistant).get("tool_calls")
    assert isinstance(tool_calls, list)
    call_items = cast("list[object]", tool_calls)
    assert call_items[0] == {"id": "call-1", "name": "echo", "arguments": '{"value": 1}'}
    tool_message = message_items[3]
    assert isinstance(tool_message, dict)
    assert cast("dict[str, object]", tool_message).get("tool_call_id") == "call-1"


def test_model_response_frame_with_tool_calls_roundtrips() -> None:
    frame = _roundtrip(
        {
            "protocol_version": HOST_PROTOCOL_VERSION,
            "type": "model_response",
            "request_id": "port-1",
            "payload": {
                "output_text": "",
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
                "tool_calls": [{"id": "call-1", "name": "echo", "arguments": '{"value": 1}'}],
            },
        }
    )
    payload = frame["payload"]
    assert isinstance(payload, dict)
    payload_item = cast("dict[str, object]", payload)
    assert payload_item["stop_reason"] == "tool_use"
    tool_calls = payload_item["tool_calls"]
    assert isinstance(tool_calls, list)
    call_items = cast("list[object]", tool_calls)
    assert isinstance(call_items[0], dict)
    assert cast("dict[str, object]", call_items[0])["arguments"] == '{"value": 1}'


def test_old_tool_free_frames_stay_valid() -> None:
    # A frame exactly like the ones peers exchanged before ADR 0042 must roundtrip
    # unchanged: the new fields are optional, never required.
    frame = _roundtrip(
        {
            "protocol_version": HOST_PROTOCOL_VERSION,
            "type": "model_request",
            "request_id": "port-1",
            "payload": {
                "messages": [
                    {"role": "system", "content": "be brief"},
                    {"role": "user", "content": "hello"},
                ],
                "model": "fake",
            },
        }
    )
    payload = frame["payload"]
    assert isinstance(payload, dict)
    assert "tools" not in payload
    assert "tool_calls" not in payload
