# ADR 0045 real-socket tests: forced tool choice and multi-turn tool conversations
# per route. Same harness pattern as the other real-socket suites.

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


def _head(env_base: str, key_env: str, path: str, extra_headers: str) -> str:
    headers_open = '    headers = {"content-type": "application/json"' + extra_headers + "}"
    return f"""
import json
import os
import sys
import urllib.error
import urllib.request

opener = urllib.request.build_opener(urllib.request.ProxyHandler({{}}))


def env():
    sys.stdin.readline()
    return os.environ[{env_base!r}], os.environ[{key_env!r}]


def post(base, key, body):
{headers_open}
    request = urllib.request.Request(
        base + {path!r},
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
    )
    with opener.open(request, timeout=30) as response:
        return response.status, json.loads(response.read().decode("utf-8"))
"""


ANTHROPIC_HEAD = _head(
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_KEY",
    "/v1/messages",
    ', "x-api-key": key, "anthropic-version": "2023-06-01"',
)

RESPONSES_HEAD = _head(
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "/responses",
    ', "authorization": "Bearer " + key',
)

CHAT_HEAD = _head(
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "/chat/completions",
    ', "authorization": "Bearer " + key',
)

GEMINI_HEAD = _head(
    "GOOGLE_GEMINI_BASE_URL",
    "GEMINI_API_KEY",
    "/v1beta/models/gemini-fake:generateContent",
    ', "x-goog-api-key": key',
)

# Per route: (a) forced tool choice; (b) multi-turn: tool exchange then a new user message.
CHILD_ANTHROPIC_FORCED = (
    ANTHROPIC_HEAD
    + """

def main():
    base, key = env()
    tools = [{"name": "echo", "description": "Echo", "input_schema": {"type": "object"}}]
    status, message = post(base, key, {
        "model": "claude-fake",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hello"}],
        "tools": tools,
        "tool_choice": {"type": "any"},
    })
    assert status == 200, status
    assert message["stop_reason"] == "tool_use", message
    block = [b for b in message["content"] if b["type"] == "tool_use"][0]
    assert block["name"] == "echo", block
    print("ANTHROPIC_FORCED_OK")


main()
"""
)

CHILD_ANTHROPIC_MULTITURN = (
    ANTHROPIC_HEAD
    + """

def main():
    base, key = env()
    status, message = post(base, key, {
        "model": "claude-fake",
        "max_tokens": 64,
        "messages": [
            {"role": "user", "content": "first turn"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "call-1", "name": "echo", "input": {}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "ok"}],
            },
            {"role": "user", "content": "second turn"},
        ],
    })
    assert status == 200, status
    assert message["content"][0]["text"] == "MULTITURN_OK", message
    print("ANTHROPIC_MULTITURN_OK")


main()
"""
)

CHILD_RESPONSES_FORCED = (
    RESPONSES_HEAD
    + """

def main():
    base, key = env()
    tools = [{"type": "function", "name": "echo", "description": "Echo",
              "parameters": {"type": "object"}}]
    status, response = post(base, key, {
        "model": "gpt-fake",
        "input": "hello",
        "tools": tools,
        "tool_choice": "required",
    })
    assert status == 200, status
    calls = [item for item in response["output"] if item["type"] == "function_call"]
    assert len(calls) == 1, response
    assert calls[0]["name"] == "echo", calls[0]
    print("RESPONSES_FORCED_OK")


main()
"""
)

CHILD_RESPONSES_MULTITURN = (
    RESPONSES_HEAD
    + """

def main():
    base, key = env()
    status, response = post(base, key, {
        "model": "gpt-fake",
        "input": [
            {"type": "message", "role": "user", "content": "first turn"},
            {"type": "function_call", "call_id": "call-1", "name": "echo",
             "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call-1", "output": "ok"},
            {"type": "message", "role": "user", "content": "second turn"},
        ],
    })
    assert status == 200, status
    assert response["output_text"] == "MULTITURN_OK", response
    print("RESPONSES_MULTITURN_OK")


main()
"""
)

CHILD_CHAT_FORCED = (
    CHAT_HEAD
    + """

def main():
    base, key = env()
    tools = [{
        "type": "function",
        "function": {"name": "echo", "description": "Echo",
                     "parameters": {"type": "object", "properties": {}}},
    }]
    status, completion = post(base, key, {
        "model": "gpt-fake",
        "messages": [{"role": "user", "content": "hello"}],
        "tools": tools,
        "tool_choice": "required",
    })
    assert status == 200, status
    message = completion["choices"][0]["message"]
    assert message["tool_calls"][0]["function"]["name"] == "echo", message
    print("CHAT_FORCED_OK")


main()
"""
)

CHILD_CHAT_MULTITURN = (
    CHAT_HEAD
    + """

def main():
    base, key = env()
    status, completion = post(base, key, {
        "model": "gpt-fake",
        "messages": [
            {"role": "user", "content": "first turn"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "call-1", "type": "function",
                             "function": {"name": "echo", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
            {"role": "user", "content": "second turn"},
        ],
    })
    assert status == 200, status
    assert completion["choices"][0]["message"]["content"] == "MULTITURN_OK", completion
    print("CHAT_MULTITURN_OK")


main()
"""
)

CHILD_GEMINI_FORCED = (
    GEMINI_HEAD
    + """

def main():
    base, key = env()
    status, document = post(base, key, {
        "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
        "tools": [{"functionDeclarations": [
            {"name": "echo", "description": "Echo",
             "parameters": {"type": "object", "properties": {}}}
        ]}],
        "toolConfig": {"functionCallingConfig": {"mode": "ANY"}},
    })
    assert status == 200, status
    parts = document["candidates"][0]["content"]["parts"]
    calls = [part for part in parts if "functionCall" in part]
    assert calls and calls[0]["functionCall"]["name"] == "echo", document
    print("GEMINI_FORCED_OK")


main()
"""
)

CHILD_GEMINI_MULTITURN = (
    GEMINI_HEAD
    + """

def main():
    base, key = env()
    status, document = post(base, key, {
        "contents": [
            {"role": "user", "parts": [{"text": "first turn"}]},
            {"role": "model", "parts": [
                {"functionCall": {"name": "echo", "args": {}}}
            ]},
            {"role": "user", "parts": [
                {"functionResponse": {"name": "echo", "response": {"output": "ok"}}}
            ]},
            {"role": "user", "parts": [{"text": "second turn"}]},
        ],
    })
    assert status == 200, status
    parts = document["candidates"][0]["content"]["parts"]
    assert parts[0].get("text") == "MULTITURN_OK", document
    print("GEMINI_MULTITURN_OK")


main()
"""
)


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
    route_model_call: Callable[[dict[str, object]], dict[str, object]],
    log: list[dict[str, object]],
    log_lock: threading.Lock,
) -> None:
    """Grant the tool, answer frames, record model_request payloads."""

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


def _text_route(text: str) -> Callable[[dict[str, object]], dict[str, object]]:
    def route(payload: dict[str, object]) -> dict[str, object]:
        return {
            "output_text": text,
            "stop_reason": "stop",
            "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        }

    return route


def _tool_route() -> Callable[[dict[str, object]], dict[str, object]]:
    def route(payload: dict[str, object]) -> dict[str, object]:
        return {
            "output_text": "",
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
            "tool_calls": [{"id": "call-1", "name": ECHO_TOOL, "arguments": "{}"}],
        }

    return route


async def _run_child(
    route_model_call: Callable[[dict[str, object]], dict[str, object]],
    child_script: str,
) -> tuple[int, str, str, list[dict[str, object]]]:
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


def _wire_choice(frames: list[dict[str, object]]) -> object:
    requests = _model_requests(frames)
    assert requests, frames
    payload = requests[0].get("payload")
    assert isinstance(payload, dict)
    return cast("dict[str, object]", payload).get("tool_choice")


@pytest.mark.anyio
async def test_anthropic_forced_any_choice_reaches_the_wire() -> None:
    code, stdout, stderr, frames = await _run_child(_tool_route(), CHILD_ANTHROPIC_FORCED)

    assert code == 0, stderr
    assert "ANTHROPIC_FORCED_OK" in stdout, stdout
    assert _wire_choice(frames) == {"kind": "required"}, frames


@pytest.mark.anyio
async def test_anthropic_multiturn_tool_conversation_succeeds() -> None:
    code, stdout, stderr, frames = await _run_child(
        _text_route("MULTITURN_OK"), CHILD_ANTHROPIC_MULTITURN
    )

    assert code == 0, stderr
    assert "ANTHROPIC_MULTITURN_OK" in stdout, stdout
    payload = _model_requests(frames)[0].get("payload")
    assert isinstance(payload, dict)
    messages = cast("dict[str, object]", payload).get("messages")
    assert isinstance(messages, list)
    roles = [
        cast("dict[str, object]", item).get("role")
        for item in cast("list[object]", messages)
        if isinstance(item, dict)
    ]
    assert roles == ["user", "assistant", "tool", "user"], roles


@pytest.mark.anyio
async def test_responses_forced_choice_reaches_the_wire() -> None:
    code, stdout, stderr, frames = await _run_child(_tool_route(), CHILD_RESPONSES_FORCED)

    assert code == 0, stderr
    assert "RESPONSES_FORCED_OK" in stdout, stdout
    assert _wire_choice(frames) == {"kind": "required"}, frames


@pytest.mark.anyio
async def test_responses_multiturn_tool_conversation_succeeds() -> None:
    code, stdout, stderr, frames = await _run_child(
        _text_route("MULTITURN_OK"), CHILD_RESPONSES_MULTITURN
    )

    assert code == 0, stderr
    assert "RESPONSES_MULTITURN_OK" in stdout, stdout
    assert len(_model_requests(frames)) == 1, frames


@pytest.mark.anyio
async def test_chat_forced_choice_reaches_the_wire() -> None:
    code, stdout, stderr, frames = await _run_child(_tool_route(), CHILD_CHAT_FORCED)

    assert code == 0, stderr
    assert "CHAT_FORCED_OK" in stdout, stdout
    assert _wire_choice(frames) == {"kind": "required"}, frames


@pytest.mark.anyio
async def test_chat_multiturn_tool_conversation_succeeds() -> None:
    code, stdout, stderr, frames = await _run_child(
        _text_route("MULTITURN_OK"), CHILD_CHAT_MULTITURN
    )

    assert code == 0, stderr
    assert "CHAT_MULTITURN_OK" in stdout, stdout
    assert len(_model_requests(frames)) == 1, frames


@pytest.mark.anyio
async def test_gemini_forced_any_mode_reaches_the_wire() -> None:
    code, stdout, stderr, frames = await _run_child(_tool_route(), CHILD_GEMINI_FORCED)

    assert code == 0, stderr
    assert "GEMINI_FORCED_OK" in stdout, stdout
    assert _wire_choice(frames) == {"kind": "required"}, frames


@pytest.mark.anyio
async def test_gemini_multiturn_tool_conversation_succeeds() -> None:
    code, stdout, stderr, frames = await _run_child(
        _text_route("MULTITURN_OK"), CHILD_GEMINI_MULTITURN
    )

    assert code == 0, stderr
    assert "GEMINI_MULTITURN_OK" in stdout, stdout
    assert len(_model_requests(frames)) == 1, frames
