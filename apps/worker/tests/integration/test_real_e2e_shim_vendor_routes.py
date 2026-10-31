# Real-socket tests for ADR 0044: Anthropic Messages, OpenAI Responses, and Gemini
# routes on the shim. Same pattern as the earlier real-socket suites: a real
# ShimServer on 127.0.0.1, a fake Worker on real wire pipes, and real stdlib-only
# children speaking each vendor's own HTTP shape over real sockets.

from __future__ import annotations

import asyncio
import os
import secrets
import sys
import threading
import time
from collections.abc import Callable
from typing import BinaryIO, cast

import pytest
from nervos_package_host.__main__ import process_agent_environment
from nervos_package_host.bridge_shim import ShimConfig, ShimServer
from nervos_package_host.wire import (
    HOST_PROTOCOL_VERSION,
    HostProtocolError,
    read_frame,
    write_frame,
)

ECHO_TOOL = "echo"

ANTHROPIC_TOOLS = """
TOOLS = [{
    "name": "echo",
    "description": "Echo tool",
    "input_schema": {"type": "object", "properties": {"value": {"type": "integer"}}},
}]
"""

ANTHROPIC_HEAD = (
    """
import json
import os
import sys
import urllib.error
import urllib.request

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def env():
    sys.stdin.readline()
    return os.environ["ANTHROPIC_BASE_URL"], os.environ["ANTHROPIC_API_KEY"]


def post(base, key, body, api_key=None):
    headers = {
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
        "x-api-key": key if api_key is None else api_key,
    }
    request = urllib.request.Request(
        base + "/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
    )
    with opener.open(request, timeout=30) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def stream_events(base, key, body):
    headers = {
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
        "x-api-key": key,
    }
    body = dict(body)
    body["stream"] = True
    request = urllib.request.Request(
        base + "/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
    )
    events = []
    with opener.open(request, timeout=30) as response:
        assert response.headers.get("Content-Type", "").startswith("text/event-stream")
        event_name = None
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if line.startswith("event: "):
                event_name = line[len("event: "):]
            elif line.startswith("data: "):
                events.append((event_name, json.loads(line[len("data: "):])))
    return events
"""
    + ANTHROPIC_TOOLS
)

# (a) Anthropic tool loop: tool_use out, tool_result back, final text.
ANTHROPIC_CHILD_LOOP = (
    ANTHROPIC_HEAD
    + """

def main():
    base, key = env()
    status, first = post(base, key, {
        "model": "claude-fake",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "run the echo tool"}],
        "tools": TOOLS,
    })
    assert status == 200, status
    assert first["type"] == "message", first
    assert first["stop_reason"] == "tool_use", first
    tool_use = [b for b in first["content"] if b["type"] == "tool_use"][0]
    assert tool_use["name"] == "echo", tool_use
    assert tool_use["input"] == {"value": 1}, tool_use
    print("ANTHROPIC_TOOL_CALL_OK")

    status, final = post(base, key, {
        "model": "claude-fake",
        "max_tokens": 100,
        "messages": [
            {"role": "user", "content": "run the echo tool"},
            {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": tool_use["id"],
                    "name": "echo",
                    "input": {"value": 1},
                }],
            },
            {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use["id"],
                    "content": "TOOL_RESULT_OK",
                }],
            },
        ],
        "tools": TOOLS,
    })
    assert status == 200, status
    assert final["stop_reason"] == "end_turn", final
    assert final["content"][0]["text"] == "FINAL_OK", final
    assert final["usage"]["input_tokens"] > 0, final
    print("FINAL_OK")


main()
"""
)

# (b) Anthropic streamed text until message_stop.
ANTHROPIC_CHILD_STREAM_TEXT = (
    ANTHROPIC_HEAD
    + """

def main():
    base, key = env()
    events = stream_events(base, key, {
        "model": "claude-fake",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hello"}],
    })
    assert events[0][0] == "message_start", events[0]
    text = "".join(
        document["delta"]["text"]
        for name, document in events
        if name == "content_block_delta" and document["delta"]["type"] == "text_delta"
    )
    deltas = [document for name, document in events if name == "message_delta"]
    assert deltas and deltas[0]["delta"]["stop_reason"] == "end_turn", events
    assert events[-1][0] == "message_stop", events[-1]
    assert text == "STREAM_OK", text
    print("ANTHROPIC_STREAM_OK")


main()
"""
)

# (c) Anthropic streamed tool call: input_json_delta reassembly.
ANTHROPIC_CHILD_STREAM_TOOL = (
    ANTHROPIC_HEAD
    + """

def main():
    base, key = env()
    events = stream_events(base, key, {
        "model": "claude-fake",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "run the echo tool"}],
        "tools": TOOLS,
    })
    starts = [
        document["content_block"]
        for name, document in events
        if name == "content_block_start" and document["content_block"]["type"] == "tool_use"
    ]
    assert len(starts) == 1, starts
    assert starts[0]["name"] == "echo", starts[0]
    arguments = "".join(
        document["delta"]["partial_json"]
        for name, document in events
        if name == "content_block_delta" and document["delta"]["type"] == "input_json_delta"
    )
    assert json.loads(arguments) == {"value": 1}, arguments
    deltas = [document for name, document in events if name == "message_delta"]
    assert deltas and deltas[0]["delta"]["stop_reason"] == "tool_use", events
    print("ANTHROPIC_TOOLSTREAM_OK")


main()
"""
)

# (d)+(e) Anthropic 400 (tool_choice any) and 401 (wrong key), vendor-shaped.
ANTHROPIC_CHILD_ERRORS = (
    ANTHROPIC_HEAD
    + """

def expect_error(base, key, body, want_status, marker, api_key=None):
    try:
        post(base, key, body, api_key=api_key)
        raise SystemExit("expected HTTP %s" % want_status)
    except urllib.error.HTTPError as error:
        assert error.code == want_status, (error.code, want_status)
        document = json.loads(error.read().decode("utf-8"))
        assert document.get("type") == "error", document
        assert document.get("error", {}).get("message"), document
    print(marker)


def main():
    base, key = env()
    expect_error(base, key, {
        "model": "claude-fake",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": TOOLS,
        "tool_choice": {"type": "tool", "name": "missing"},
    }, 400, "ANTHROPIC_CHOICE_400")
    expect_error(base, key, {
        "model": "claude-fake",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
    }, 401, "ANTHROPIC_KEY_401", api_key="wrong-key")


main()
"""
)

RESPONSES_HEAD = (
    """
import json
import os
import sys
import urllib.error
import urllib.request

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def env():
    sys.stdin.readline()
    return os.environ["OPENAI_BASE_URL"], os.environ["OPENAI_API_KEY"]


def post(base, key, body, api_key=None):
    headers = {
        "content-type": "application/json",
        "authorization": "Bearer " + (key if api_key is None else api_key),
    }
    request = urllib.request.Request(
        base + "/responses",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
    )
    with opener.open(request, timeout=30) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def stream_events(base, key, body):
    headers = {
        "content-type": "application/json",
        "authorization": "Bearer " + key,
    }
    body = dict(body)
    body["stream"] = True
    request = urllib.request.Request(
        base + "/responses",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
    )
    events = []
    with opener.open(request, timeout=30) as response:
        assert response.headers.get("Content-Type", "").startswith("text/event-stream")
        event_name = None
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if line.startswith("event: "):
                event_name = line[len("event: "):]
            elif line.startswith("data: "):
                events.append((event_name, json.loads(line[len("data: "):])))
    return events
"""
    + """
TOOLS = [{
    "type": "function",
    "name": "echo",
    "description": "Echo tool",
    "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}},
}]
"""
)

# (a) Responses tool loop: function_call out, function_call_output back, final text.
RESPONSES_CHILD_LOOP = (
    RESPONSES_HEAD
    + """

def main():
    base, key = env()
    status, first = post(base, key, {
        "model": "gpt-fake",
        "instructions": "be brief",
        "input": "run the echo tool",
        "tools": TOOLS,
        "store": False,
    })
    assert status == 200, status
    assert first["object"] == "response", first
    assert first["status"] == "completed", first
    calls = [item for item in first["output"] if item["type"] == "function_call"]
    assert len(calls) == 1, first
    call = calls[0]
    assert call["name"] == "echo", call
    assert json.loads(call["arguments"]) == {"value": 1}, call
    print("RESPONSES_TOOL_CALL_OK")

    status, final = post(base, key, {
        "model": "gpt-fake",
        "instructions": "be brief",
        "input": [
            {"type": "message", "role": "user", "content": "run the echo tool"},
            {
                "type": "function_call",
                "call_id": call["call_id"],
                "name": "echo",
                "arguments": '{"value": 1}',
            },
            {
                "type": "function_call_output",
                "call_id": call["call_id"],
                "output": "TOOL_RESULT_OK",
            },
        ],
        "tools": TOOLS,
    })
    assert status == 200, status
    assert final["output_text"] == "FINAL_OK", final
    assert final["usage"]["total_tokens"] > 0, final
    print("FINAL_OK")


main()
"""
)

# (b) Responses streamed text until response.completed.
RESPONSES_CHILD_STREAM_TEXT = (
    RESPONSES_HEAD
    + """

def main():
    base, key = env()
    events = stream_events(base, key, {
        "model": "gpt-fake",
        "input": "hello",
    })
    assert events[0][0] == "response.created", events[0]
    text = "".join(
        document["delta"]
        for name, document in events
        if name == "response.output_text.delta"
    )
    dones = [document for name, document in events if name == "response.output_text.done"]
    assert dones and dones[0]["text"] == "STREAM_OK", events
    assert events[-1][0] == "response.completed", events[-1]
    assert text == "STREAM_OK", text
    completed = events[-1][1]["response"]
    assert completed["status"] == "completed", completed
    print("RESPONSES_STREAM_OK")


main()
"""
)

# (c) Responses streamed function call: arguments delta reassembly.
RESPONSES_CHILD_STREAM_TOOL = (
    RESPONSES_HEAD
    + """

def main():
    base, key = env()
    events = stream_events(base, key, {
        "model": "gpt-fake",
        "input": "run the echo tool",
        "tools": TOOLS,
    })
    arguments = "".join(
        document["delta"]
        for name, document in events
        if name == "response.function_call_arguments.delta"
    )
    assert json.loads(arguments) == {"value": 1}, arguments
    added = [
        document["item"]
        for name, document in events
        if name == "response.output_item.added"
        and document["item"]["type"] == "function_call"
    ]
    assert len(added) == 1, added
    assert added[0]["name"] == "echo", added[0]
    assert events[-1][0] == "response.completed", events[-1]
    print("RESPONSES_TOOLSTREAM_OK")


main()
"""
)

# (d)+(e) Responses 400 (image input) and 401 (wrong key), OpenAI-shaped.
RESPONSES_CHILD_ERRORS = (
    RESPONSES_HEAD
    + """

def expect_error(base, key, body, want_status, marker, api_key=None):
    try:
        post(base, key, body, api_key=api_key)
        raise SystemExit("expected HTTP %s" % want_status)
    except urllib.error.HTTPError as error:
        assert error.code == want_status, (error.code, want_status)
        document = json.loads(error.read().decode("utf-8"))
        assert "error" in document, document
        assert document["error"].get("message"), document
    print(marker)


def main():
    base, key = env()
    expect_error(base, key, {
        "model": "gpt-fake",
        "input": [{
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "look"},
                {"type": "input_image", "image_url": "http://127.0.0.1/x.png"},
            ],
        }],
    }, 400, "RESPONSES_IMAGE_400")
    expect_error(base, key, {
        "model": "gpt-fake",
        "input": "hi",
        "previous_response_id": "resp_old",
    }, 400, "RESPONSES_PREVIOUS_400")
    expect_error(base, key, {
        "model": "gpt-fake",
        "input": "hi",
    }, 401, "RESPONSES_KEY_401", api_key="wrong-key")


main()
"""
)

GEMINI_HEAD = (
    """
import json
import os
import sys
import urllib.error
import urllib.request

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def env():
    sys.stdin.readline()
    return os.environ["GOOGLE_GEMINI_BASE_URL"], os.environ["GEMINI_API_KEY"]


def post(base, key, method, body, api_key=None):
    headers = {
        "content-type": "application/json",
        "x-goog-api-key": key if api_key is None else api_key,
    }
    request = urllib.request.Request(
        base + "/v1beta/models/gemini-fake:" + method,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
    )
    with opener.open(request, timeout=30) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def stream_chunks(base, key, body, api_key=None):
    headers = {
        "content-type": "application/json",
        "x-goog-api-key": key if api_key is None else api_key,
    }
    request = urllib.request.Request(
        base + "/v1beta/models/gemini-fake:streamGenerateContent?alt=sse",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
    )
    chunks = []
    with opener.open(request, timeout=30) as response:
        assert response.headers.get("Content-Type", "").startswith("text/event-stream")
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if line.startswith("data: "):
                chunks.append(json.loads(line[len("data: "):]))
    return chunks
"""
    + """
TOOLS = [{
    "functionDeclarations": [{
        "name": "echo",
        "description": "Echo tool",
        "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}},
    }]
}]
"""
)

# (a) Gemini tool loop: functionCall out, functionResponse back, final text.
GEMINI_CHILD_LOOP = (
    GEMINI_HEAD
    + """

def main():
    base, key = env()
    status, first = post(base, key, "generateContent", {
        "systemInstruction": {"parts": [{"text": "be brief"}]},
        "contents": [{"role": "user", "parts": [{"text": "run the echo tool"}]}],
        "tools": TOOLS,
    })
    assert status == 200, status
    candidate = first["candidates"][0]
    assert candidate["finishReason"] == "STOP", first
    calls = [p["functionCall"] for p in candidate["content"]["parts"] if "functionCall" in p]
    assert len(calls) == 1, first
    assert calls[0]["name"] == "echo", calls[0]
    assert calls[0]["args"] == {"value": 1}, calls[0]
    assert first["usageMetadata"]["promptTokenCount"] > 0, first
    print("GEMINI_TOOL_CALL_OK")

    status, final = post(base, key, "generateContent", {
        "contents": [
            {"role": "user", "parts": [{"text": "run the echo tool"}]},
            {"role": "model", "parts": [
                {"functionCall": {"name": "echo", "args": {"value": 1}}}
            ]},
            {"role": "user", "parts": [
                {"functionResponse": {"name": "echo", "response": {"output": "TOOL_RESULT_OK"}}}
            ]},
        ],
        "tools": TOOLS,
    })
    assert status == 200, status
    assert final["candidates"][0]["content"]["parts"][0]["text"] == "FINAL_OK", final
    assert final["candidates"][0]["finishReason"] == "STOP", final
    print("FINAL_OK")


main()
"""
)

# (b) Gemini streamed text until the finishReason chunk.
GEMINI_CHILD_STREAM_TEXT = (
    GEMINI_HEAD
    + """

def main():
    base, key = env()
    chunks = stream_chunks(base, key, {
        "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
    })
    text = "".join(
        part["text"]
        for chunk in chunks
        for part in chunk.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        if "text" in part
    )
    finishes = [
        chunk for chunk in chunks
        if chunk.get("candidates", [{}])[0].get("finishReason")
    ]
    assert finishes and finishes[0]["candidates"][0]["finishReason"] == "STOP", chunks
    assert text == "STREAM_OK", text
    assert chunks[-1].get("usageMetadata", {}).get("totalTokenCount", 0) > 0, chunks[-1]
    print("GEMINI_STREAM_OK")


main()
"""
)

# (c) Gemini streamed function call.
GEMINI_CHILD_STREAM_TOOL = (
    GEMINI_HEAD
    + """

def main():
    base, key = env()
    chunks = stream_chunks(base, key, {
        "contents": [{"role": "user", "parts": [{"text": "run the echo tool"}]}],
        "tools": TOOLS,
    })
    calls = [
        part["functionCall"]
        for chunk in chunks
        for part in chunk.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        if "functionCall" in part
    ]
    assert len(calls) == 1, calls
    assert calls[0]["name"] == "echo", calls[0]
    assert calls[0]["args"] == {"value": 1}, calls[0]
    finishes = [
        chunk for chunk in chunks
        if chunk.get("candidates", [{}])[0].get("finishReason")
    ]
    assert finishes, chunks
    print("GEMINI_TOOLSTREAM_OK")


main()
"""
)

# (d)+(e) Gemini 400 (toolConfig ANY) and 401 (wrong key), Gemini-shaped.
GEMINI_CHILD_ERRORS = (
    GEMINI_HEAD
    + """

def expect_error(base, key, body, want_status, marker, api_key=None):
    try:
        post(base, key, "generateContent", body, api_key=api_key)
        raise SystemExit("expected HTTP %s" % want_status)
    except urllib.error.HTTPError as error:
        assert error.code == want_status, (error.code, want_status)
        document = json.loads(error.read().decode("utf-8"))
        error_body = document.get("error", {})
        assert error_body.get("code") == want_status, document
        assert error_body.get("status"), document
        assert error_body.get("message"), document
    print(marker)


def main():
    base, key = env()
    expect_error(base, key, {
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        "tools": TOOLS,
        "toolConfig": {
            "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["missing"]}
        },
    }, 400, "GEMINI_CHOICE_400")
    expect_error(base, key, {
        "contents": [{"role": "user", "parts": [
            {"text": "look"},
            {"inlineData": {"mimeType": "image/png", "data": "aGk="}},
        ]}],
    }, 400, "GEMINI_IMAGE_400")
    expect_error(base, key, {
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
    }, 401, "GEMINI_KEY_401", api_key="wrong-key")


main()
"""
)

CHILD_OPENAI_MODELS = """
import json
import os
import sys
import urllib.request

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def main():
    sys.stdin.readline()
    base = os.environ["OPENAI_BASE_URL"]
    key = os.environ["OPENAI_API_KEY"]
    request = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps({
            "model": "discovered-model",
            "messages": [{"role": "user", "content": "hi"}],
        }).encode("utf-8"),
        headers={"content-type": "application/json", "authorization": "Bearer " + key},
    )
    with opener.open(request, timeout=30) as response:
        assert response.status == 200

    request = urllib.request.Request(base + "/models", headers={
        "authorization": "Bearer " + key,
    })
    with opener.open(request, timeout=30) as response:
        document = json.loads(response.read().decode("utf-8"))
    assert document["object"] == "list", document
    assert document["data"][0]["id"] == "discovered-model", document
    print("OPENAI_MODELS_OK")


main()
"""

CHILD_GEMINI_MODELS = """
import json
import os
import sys
import urllib.request

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def main():
    sys.stdin.readline()
    base = os.environ["GOOGLE_GEMINI_BASE_URL"]
    key = os.environ["GEMINI_API_KEY"]
    request = urllib.request.Request(base + "/v1beta/models", headers={
        "x-goog-api-key": key,
    })
    with opener.open(request, timeout=30) as response:
        document = json.loads(response.read().decode("utf-8"))
    models = document["models"]
    assert len(models) == 1, document
    # No model call happened yet, so discovery falls back to the fixed name.
    assert models[0]["name"] == "models/nervos-model", models
    assert "generateContent" in models[0]["supportedGenerationMethods"], models
    print("GEMINI_MODELS_OK")


main()
"""

CHILD_ENV = """
import os
import sys


def main():
    sys.stdin.readline()
    origin = os.environ["OPENAI_BASE_URL"][: -len("/v1")]
    assert os.environ["ANTHROPIC_BASE_URL"] == origin
    assert os.environ["GOOGLE_GEMINI_BASE_URL"] == origin
    key = os.environ["OPENAI_API_KEY"]
    assert os.environ["ANTHROPIC_API_KEY"] == key
    assert os.environ["GEMINI_API_KEY"] == key
    assert os.environ["GOOGLE_API_KEY"] == key
    assert os.environ["NERVOS_MCP_URL"] == origin + "/mcp"
    print("ENV_OK")


main()
"""

type ModelRoute = Callable[[dict[str, object]], dict[str, object]]


def _frame(message_type: str, request_id: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "protocol_version": HOST_PROTOCOL_VERSION,
        "type": message_type,
        "request_id": request_id,
        "payload": payload,
    }


def _fake_worker(
    read_stream: BinaryIO,
    write_stream: BinaryIO,
    route_model_call: ModelRoute,
    log: list[dict[str, object]],
    log_lock: threading.Lock,
) -> None:
    """Stand in for the Worker: grant the tools, answer frames, record requests."""

    def send(message_type: str, request_id: str, payload: dict[str, object]) -> None:
        write_frame(write_stream, _frame(message_type, request_id, payload))

    send(
        "initialize",
        "init-1",
        {
            "runtime_kind": "process",
            "command": ["bin/agent"],
            "tools": [
                {
                    "name": ECHO_TOOL,
                    "description": "Echo tool",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
        },
    )
    while True:
        try:
            message = read_frame(read_stream)
        except (HostProtocolError, EOFError, OSError):
            return
        with log_lock:
            log.append(message)
        message_type = message.get("type")
        request_id = message.get("request_id")
        payload = message.get("payload")
        if not isinstance(request_id, str) or not isinstance(payload, dict):
            return
        if message_type == "model_request":
            send(
                "model_response",
                request_id,
                route_model_call(cast("dict[str, object]", payload)),
            )


def _tool_loop_route(payload: dict[str, object]) -> dict[str, object]:
    """Answer the first user turn with a tool call, the tool report with text."""
    roles = _message_roles(payload)
    usage = {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}
    if roles and roles[-1] == "tool":
        return {"output_text": "FINAL_OK", "stop_reason": "stop", "usage": usage}
    return {
        "output_text": "",
        "stop_reason": "tool_use",
        "usage": usage,
        "tool_calls": [{"id": "call-1", "name": ECHO_TOOL, "arguments": '{"value": 1}'}],
    }


def _text_route(text: str) -> ModelRoute:
    def route(payload: dict[str, object]) -> dict[str, object]:
        return {
            "output_text": text,
            "stop_reason": "stop",
            "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        }

    return route


def _tool_route() -> ModelRoute:
    def route(payload: dict[str, object]) -> dict[str, object]:
        return {
            "output_text": "",
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
            "tool_calls": [{"id": "call-1", "name": ECHO_TOOL, "arguments": '{"value": 1}'}],
        }

    return route


async def _run_child(
    route_model_call: ModelRoute,
    child_script: str,
) -> tuple[int, str, str, list[dict[str, object]]]:
    """Serve the shim, answer model calls via ``route_model_call``, run the child."""
    token = "e2e-" + secrets.token_hex(16)
    to_shim_read_fd, to_shim_write_fd = os.pipe()
    from_shim_read_fd, from_shim_write_fd = os.pipe()
    wire_in = cast("BinaryIO", os.fdopen(to_shim_read_fd, "rb", buffering=0))
    worker_write = cast("BinaryIO", os.fdopen(to_shim_write_fd, "wb", buffering=0))
    worker_read = cast("BinaryIO", os.fdopen(from_shim_read_fd, "rb", buffering=0))
    wire_out = cast("BinaryIO", os.fdopen(from_shim_write_fd, "wb", buffering=0))

    async def noop_handler(message: dict[str, object]) -> None:
        return None

    server = ShimServer(
        ShimConfig(token=token, request_timeout=30.0, max_requests=100),
        wire_in,
        wire_out,
        noop_handler,
    )
    server.start()
    origin = f"http://127.0.0.1:{server.port}"

    log: list[dict[str, object]] = []
    log_lock = threading.Lock()
    worker_thread = threading.Thread(
        target=_fake_worker,
        args=(worker_read, worker_write, route_model_call, log, log_lock),
        daemon=True,
    )
    worker_thread.start()

    deadline = time.monotonic() + 5.0
    while not server.state.tools_info:
        if time.monotonic() >= deadline:
            pytest.fail("shim did not consume the initialize frame granting tools")
        await asyncio.sleep(0.01)

    # The child sees exactly the environment the real process-agent host builds.
    env = os.environ.copy()
    env.update(process_agent_environment(origin, token))
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        child_script,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    assert process.stdin is not None
    process.stdin.write(b'{"input": "run"}\n')
    await process.stdin.drain()
    process.stdin.close()
    stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=90.0)
    await server.stop()
    worker_write.close()
    wire_in.close()
    wire_out.close()
    worker_thread.join(timeout=10.0)
    worker_read.close()
    with log_lock:
        frames = list(log)
    return (
        process.returncode if process.returncode is not None else -1,
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
        frames,
    )


def _model_requests(frames: list[dict[str, object]]) -> list[dict[str, object]]:
    return [frame for frame in frames if frame.get("type") == "model_request"]


def _message_roles(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return []
    messages = cast("dict[str, object]", payload).get("messages")
    if not isinstance(messages, list):
        return []
    roles: list[str] = []
    for message in cast("list[object]", messages):
        if isinstance(message, dict):
            role = cast("dict[str, object]", message).get("role")
            if isinstance(role, str):
                roles.append(role)
    return roles


# --------------------------------------------------------------------------- Anthropic


@pytest.mark.anyio
async def test_anthropic_tool_calling_loop_over_real_sockets() -> None:
    code, stdout, stderr, frames = await _run_child(_tool_loop_route, ANTHROPIC_CHILD_LOOP)

    assert code == 0, stderr
    assert "ANTHROPIC_TOOL_CALL_OK" in stdout, stdout
    assert "FINAL_OK" in stdout, stdout
    requests = _model_requests(frames)
    assert len(requests) == 2, frames
    second_payload = requests[1].get("payload")
    roles = _message_roles(second_payload)
    assert roles[-2:] == ["assistant", "tool"], roles


@pytest.mark.anyio
async def test_anthropic_streaming_text_until_message_stop() -> None:
    code, stdout, stderr, frames = await _run_child(
        _text_route("STREAM_OK"), ANTHROPIC_CHILD_STREAM_TEXT
    )

    assert code == 0, stderr
    assert "ANTHROPIC_STREAM_OK" in stdout, stdout
    assert len(_model_requests(frames)) == 1, frames


@pytest.mark.anyio
async def test_anthropic_streaming_tool_call_reassembles_input_json_delta() -> None:
    code, stdout, stderr, frames = await _run_child(_tool_route(), ANTHROPIC_CHILD_STREAM_TOOL)

    assert code == 0, stderr
    assert "ANTHROPIC_TOOLSTREAM_OK" in stdout, stdout
    assert len(_model_requests(frames)) == 1, frames


@pytest.mark.anyio
async def test_anthropic_rejections_never_reach_the_wire() -> None:
    code, stdout, stderr, frames = await _run_child(_tool_loop_route, ANTHROPIC_CHILD_ERRORS)

    assert code == 0, stderr
    assert "ANTHROPIC_CHOICE_400" in stdout, stdout
    assert "ANTHROPIC_KEY_401" in stdout, stdout
    assert _model_requests(frames) == [], frames


# ---------------------------------------------------------------------------- Responses


@pytest.mark.anyio
async def test_responses_tool_calling_loop_over_real_sockets() -> None:
    code, stdout, stderr, frames = await _run_child(_tool_loop_route, RESPONSES_CHILD_LOOP)

    assert code == 0, stderr
    assert "RESPONSES_TOOL_CALL_OK" in stdout, stdout
    assert "FINAL_OK" in stdout, stdout
    requests = _model_requests(frames)
    assert len(requests) == 2, frames
    first_payload = requests[0].get("payload")
    roles = _message_roles(first_payload)
    assert roles[0] == "system", roles
    second_payload = requests[1].get("payload")
    second_roles = _message_roles(second_payload)
    assert second_roles[-2:] == ["assistant", "tool"], second_roles


@pytest.mark.anyio
async def test_responses_streaming_text_until_completed() -> None:
    code, stdout, stderr, frames = await _run_child(
        _text_route("STREAM_OK"), RESPONSES_CHILD_STREAM_TEXT
    )

    assert code == 0, stderr
    assert "RESPONSES_STREAM_OK" in stdout, stdout
    assert len(_model_requests(frames)) == 1, frames


@pytest.mark.anyio
async def test_responses_streaming_tool_call_reassembles_arguments() -> None:
    code, stdout, stderr, frames = await _run_child(_tool_route(), RESPONSES_CHILD_STREAM_TOOL)

    assert code == 0, stderr
    assert "RESPONSES_TOOLSTREAM_OK" in stdout, stdout
    assert len(_model_requests(frames)) == 1, frames


@pytest.mark.anyio
async def test_responses_rejections_never_reach_the_wire() -> None:
    code, stdout, stderr, frames = await _run_child(_tool_loop_route, RESPONSES_CHILD_ERRORS)

    assert code == 0, stderr
    assert "RESPONSES_IMAGE_400" in stdout, stdout
    assert "RESPONSES_PREVIOUS_400" in stdout, stdout
    assert "RESPONSES_KEY_401" in stdout, stdout
    assert _model_requests(frames) == [], frames


# ------------------------------------------------------------------------------- Gemini


@pytest.mark.anyio
async def test_gemini_tool_calling_loop_over_real_sockets() -> None:
    code, stdout, stderr, frames = await _run_child(_tool_loop_route, GEMINI_CHILD_LOOP)

    assert code == 0, stderr
    assert "GEMINI_TOOL_CALL_OK" in stdout, stdout
    assert "FINAL_OK" in stdout, stdout
    requests = _model_requests(frames)
    assert len(requests) == 2, frames
    second_payload = requests[1].get("payload")
    roles = _message_roles(second_payload)
    assert roles[-2:] == ["assistant", "tool"], roles


@pytest.mark.anyio
async def test_gemini_streaming_text_until_finish_reason() -> None:
    code, stdout, stderr, frames = await _run_child(
        _text_route("STREAM_OK"), GEMINI_CHILD_STREAM_TEXT
    )

    assert code == 0, stderr
    assert "GEMINI_STREAM_OK" in stdout, stdout
    assert len(_model_requests(frames)) == 1, frames


@pytest.mark.anyio
async def test_gemini_streaming_tool_call_reassembles_function_call() -> None:
    code, stdout, stderr, frames = await _run_child(_tool_route(), GEMINI_CHILD_STREAM_TOOL)

    assert code == 0, stderr
    assert "GEMINI_TOOLSTREAM_OK" in stdout, stdout
    assert len(_model_requests(frames)) == 1, frames


@pytest.mark.anyio
async def test_gemini_rejections_never_reach_the_wire() -> None:
    code, stdout, stderr, frames = await _run_child(_tool_loop_route, GEMINI_CHILD_ERRORS)

    assert code == 0, stderr
    assert "GEMINI_CHOICE_400" in stdout, stdout
    assert "GEMINI_IMAGE_400" in stdout, stdout
    assert "GEMINI_KEY_401" in stdout, stdout
    assert _model_requests(frames) == [], frames


# ----------------------------------------------------------------------- Discovery + env


@pytest.mark.anyio
async def test_openai_models_discovery_lists_the_last_model_used() -> None:
    code, stdout, stderr, frames = await _run_child(_text_route("hi"), CHILD_OPENAI_MODELS)

    assert code == 0, stderr
    assert "OPENAI_MODELS_OK" in stdout, stdout
    assert len(_model_requests(frames)) == 1, frames


@pytest.mark.anyio
async def test_gemini_models_discovery_falls_back_to_nervos_model() -> None:
    code, stdout, stderr, frames = await _run_child(_text_route("hi"), CHILD_GEMINI_MODELS)

    assert code == 0, stderr
    assert "GEMINI_MODELS_OK" in stdout, stdout
    assert _model_requests(frames) == [], frames


@pytest.mark.anyio
async def test_process_agent_environment_is_set_for_the_child() -> None:
    code, stdout, stderr, frames = await _run_child(_text_route("hi"), CHILD_ENV)

    assert code == 0, stderr
    assert "ENV_OK" in stdout, stdout
    assert _model_requests(frames) == [], frames
