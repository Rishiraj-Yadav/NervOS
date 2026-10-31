# Real-socket tests for ADR 0042: function calling and SSE replay through the shim.
# Same pattern as test_real_e2e_shim_child.py: a real ShimServer on 127.0.0.1, a
# fake Worker on real wire pipes, and a real stdlib-only child over real sockets.

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
from nervos_package_host.bridge_shim import ShimConfig, ShimServer
from nervos_package_host.wire import (
    HOST_PROTOCOL_VERSION,
    HostProtocolError,
    read_frame,
    write_frame,
)

ECHO_TOOL = "echo"

_COMMON_HEAD = """
import json
import os
import sys
import urllib.error
import urllib.request

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(url, body, token):
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    )
    with opener.open(request, timeout=30) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def env():
    sys.stdin.readline()  # the host sends the run input on stdin; consume it
    return os.environ["OPENAI_BASE_URL"], os.environ["OPENAI_API_KEY"]
"""

# (a) A tool-calling loop: tools in, tool_calls out, tool result back, final text.
CHILD_TOOL_LOOP = (
    _COMMON_HEAD
    + """

def main():
    base_url, token = env()
    tools = [{
        "type": "function",
        "function": {
            "name": "echo",
            "description": "Echo tool",
            "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}},
        },
    }]
    messages = [{"role": "user", "content": "run the echo tool"}]
    status, first = post(base_url + "/chat/completions", {
        "model": "fake", "messages": messages, "tools": tools}, token)
    assert status == 200, status
    choice = first["choices"][0]
    assert choice["finish_reason"] == "tool_calls", choice
    calls = choice["message"]["tool_calls"]
    assert len(calls) == 1, calls
    call = calls[0]
    assert call["type"] == "function", call
    assert call["id"] == "call-1", call
    assert call["function"]["name"] == "echo", call
    assert json.loads(call["function"]["arguments"]) == {"value": 1}, call
    print("TOOL_CALL_OK")

    messages.append({"role": "assistant", "content": None, "tool_calls": calls})
    messages.append({"role": "tool", "tool_call_id": "call-1", "content": "TOOL_RESULT_OK"})
    status, final = post(base_url + "/chat/completions", {
        "model": "fake", "messages": messages, "tools": tools}, token)
    assert status == 200, status
    final_choice = final["choices"][0]
    assert final_choice["finish_reason"] == "stop", final_choice
    assert final_choice["message"]["content"] == "FINAL_OK", final_choice
    print(final_choice["message"]["content"])


main()
"""
)

# (b1) Streaming a text answer: SSE until [DONE], text reassembled, usage included.
CHILD_STREAM_TEXT = (
    _COMMON_HEAD
    + """

def main():
    base_url, token = env()
    body = {
        "model": "fake",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    request = urllib.request.Request(
        base_url + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    )
    with opener.open(request, timeout=30) as response:
        content_type = response.headers.get("Content-Type", "")
        assert content_type.startswith("text/event-stream"), content_type
        chunks = []
        finish = None
        usage = None
        done = False
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if not line.startswith("data: "):
                continue
            data = line[len("data: "):]
            if data == "[DONE]":
                done = True
                break
            document = json.loads(data)
            for choice in document.get("choices", []):
                delta = choice.get("delta", {})
                if delta.get("content"):
                    chunks.append(delta["content"])
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
            if document.get("usage"):
                usage = document["usage"]
        assert done, "stream ended without [DONE]"
        assert finish == "stop", finish
        assert usage is not None and usage.get("total_tokens", 0) > 0, usage
    print("".join(chunks))
    print("USAGE_OK")


main()
"""
)

# (b2) Streaming a tool call: SSE deltas with indexes reassemble id/name/arguments.
CHILD_STREAM_TOOL = (
    _COMMON_HEAD
    + """

def main():
    base_url, token = env()
    tools = [{
        "type": "function",
        "function": {"name": "echo", "description": "Echo tool", "parameters": {"type": "object"}},
    }]
    body = {
        "model": "fake",
        "messages": [{"role": "user", "content": "run the echo tool"}],
        "tools": tools,
        "stream": True,
    }
    request = urllib.request.Request(
        base_url + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    )
    with opener.open(request, timeout=30) as response:
        tool_calls = {}
        finish = None
        done = False
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if not line.startswith("data: "):
                continue
            data = line[len("data: "):]
            if data == "[DONE]":
                done = True
                break
            document = json.loads(data)
            for choice in document.get("choices", []):
                delta = choice.get("delta", {})
                for tool_call in delta.get("tool_calls") or []:
                    index = tool_call.get("index", 0)
                    entry = tool_calls.setdefault(
                        index, {"id": None, "name": None, "arguments": ""}
                    )
                    if tool_call.get("id"):
                        entry["id"] = tool_call["id"]
                    function = tool_call.get("function") or {}
                    if function.get("name"):
                        entry["name"] = function["name"]
                    entry["arguments"] += function.get("arguments", "")
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
        assert done, "stream ended without [DONE]"
        assert finish == "tool_calls", finish
        call = tool_calls[0]
        assert call["id"] == "call-1", call
        assert call["name"] == "echo", call
        assert json.loads(call["arguments"]) == {"value": 1}, call
    print("TOOLSTREAM_OK")


main()
"""
)

# (c) Clear OpenAI-style 400/413 rejections: images, unsupported tool_choice, size.
CHILD_ERRORS = (
    _COMMON_HEAD
    + """

def expect_error(body, want_status, marker):
    base_url, token = env()
    request = urllib.request.Request(
        base_url + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    )
    try:
        opener.open(request, timeout=30)
        raise SystemExit("expected HTTP %s" % want_status)
    except urllib.error.HTTPError as error:
        assert error.code == want_status, (error.code, want_status)
    print(marker)


def main():
    base_url, token = env()
    expect_error({
        "model": "fake",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": "http://127.0.0.1/x.png"}},
            ],
        }],
    }, 400, "IMAGE_400")
    # ADR 0045: "required" is now honored; a named choice for an unoffered tool
    # is still refused with 400.
    expect_error({
        "model": "fake",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "echo"}}],
        "tool_choice": {"type": "function", "name": "missing"},
    }, 400, "CHOICE_400")

    body = json.dumps({"model": "fake", "messages": [{"role": "user", "content": "hi"}]}).encode()
    request = urllib.request.Request(
        base_url + "/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
            "Content-Length": str(1024 * 1024 + 1),
        },
    )
    try:
        opener.open(request, timeout=30)
        raise SystemExit("expected HTTP 413")
    except urllib.error.HTTPError as error:
        assert error.code == 413, error.code
    print("SIZE_413")


main()
"""
)


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
    port = server.port

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
    env.update(
        {
            "OPENAI_BASE_URL": f"http://127.0.0.1:{port}/v1",
            "OPENAI_API_KEY": token,
            "NERVOS_MCP_URL": f"http://127.0.0.1:{port}/mcp",
        }
    )
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


@pytest.mark.anyio
async def test_real_tool_calling_loop_over_real_sockets() -> None:
    code, stdout, stderr, frames = await _run_child(_tool_loop_route, CHILD_TOOL_LOOP)

    assert code == 0, stderr
    assert "TOOL_CALL_OK" in stdout, stdout
    assert "FINAL_OK" in stdout, stdout
    requests = _model_requests(frames)
    assert len(requests) == 2, frames
    first_payload = requests[0].get("payload")
    assert isinstance(first_payload, dict)
    tools = cast("dict[str, object]", first_payload).get("tools")
    assert isinstance(tools, list) and tools, first_payload
    second_payload = requests[1].get("payload")
    assert isinstance(second_payload, dict)
    roles = _message_roles(cast("dict[str, object]", second_payload))
    assert roles[-2:] == ["assistant", "tool"], roles
    second_messages = cast("dict[str, object]", second_payload).get("messages")
    assert isinstance(second_messages, list)
    tool_message = cast("list[object]", second_messages)[-1]
    assert isinstance(tool_message, dict)
    assert cast("dict[str, object]", tool_message).get("tool_call_id") == "call-1"


@pytest.mark.anyio
async def test_real_streaming_text_reassembles_until_done() -> None:
    def route(payload: dict[str, object]) -> dict[str, object]:
        return {
            "output_text": "STREAM_OK",
            "stop_reason": "stop",
            "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        }

    code, stdout, stderr, frames = await _run_child(route, CHILD_STREAM_TEXT)

    assert code == 0, stderr
    assert "STREAM_OK" in stdout, stdout
    assert "USAGE_OK" in stdout, stdout
    assert len(_model_requests(frames)) == 1, frames


@pytest.mark.anyio
async def test_real_streaming_tool_call_reassembles_deltas() -> None:
    def route(payload: dict[str, object]) -> dict[str, object]:
        return {
            "output_text": "",
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
            "tool_calls": [{"id": "call-1", "name": ECHO_TOOL, "arguments": '{"value": 1}'}],
        }

    code, stdout, stderr, frames = await _run_child(route, CHILD_STREAM_TOOL)

    assert code == 0, stderr
    assert "TOOLSTREAM_OK" in stdout, stdout
    assert len(_model_requests(frames)) == 1, frames


@pytest.mark.anyio
async def test_real_rejections_never_reach_the_model() -> None:
    def route(payload: dict[str, object]) -> dict[str, object]:
        return {"output_text": "unused", "stop_reason": "stop", "usage": {}}

    code, stdout, stderr, frames = await _run_child(route, CHILD_ERRORS)

    assert code == 0, stderr
    assert "IMAGE_400" in stdout, stdout
    assert "CHOICE_400" in stdout, stdout
    assert "SIZE_413" in stdout, stdout
    # No rejected request ever crossed the wire to the Worker.
    assert _model_requests(frames) == [], frames
