# Real end-to-end test for the generic process agent bridge: a real ShimServer
# listening on 127.0.0.1, driven through real wire pipes by a fake Worker, with a
# real child process acting as a foreign agent over real HTTP sockets.

from __future__ import annotations

import asyncio
import os
import secrets
import sys
import threading
import time
from typing import BinaryIO, cast

import pytest
from nervos_package_host.bridge_shim import ShimConfig, ShimServer
from nervos_package_host.wire import (
    HOST_PROTOCOL_VERSION,
    HostProtocolError,
    read_frame,
    write_frame,
)

MODEL_ANSWER = "MODEL_OK"
TOOL_ECHO_ANSWER = "TOOL_OK"
ECHO_TOOL = "echo"
PING_TOOL = "ping"
UNGRANTED_TOOL = "secret_tool"
AUTH_MARKER = "AUTH_401_OK"
DENIED_MARKER = "DENIED_OK"

# A "foreign agent": stdlib only, no NervOS imports. It discovers the shim from
# its environment exactly like a process launched by _run_process_agent would.
CHILD_SCRIPT = """
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
        headers={"Content-Type": "application/json"},
    )
    if token is not None:
        request.add_header("Authorization", "Bearer " + token)
    with opener.open(request, timeout=30) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def main():
    sys.stdin.readline()  # the host sends the run input on stdin; consume it
    base_url = os.environ["OPENAI_BASE_URL"]
    mcp_url = os.environ["NERVOS_MCP_URL"]
    token = os.environ["OPENAI_API_KEY"]

    # (a) chat completions carrying the bearer key from the environment
    status, completion = post(
        base_url + "/chat/completions",
        {"model": "fake-model", "messages": [{"role": "user", "content": "hello"}]},
        token,
    )
    assert status == 200, status
    model_text = completion["choices"][0]["message"]["content"]

    # the same request without the bearer key must be rejected with 401
    try:
        post(
            base_url + "/chat/completions",
            {"model": "fake-model", "messages": [{"role": "user", "content": "hello"}]},
            None,
        )
        raise SystemExit("expected HTTP 401 without bearer token")
    except urllib.error.HTTPError as error:
        assert error.code == 401, error.code
    print("AUTH_401_OK")

    # (b) MCP initialize, tools/list, tools/call for a granted tool
    status, _ = post(
        mcp_url, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, token
    )
    assert status == 200, status
    status, listing = post(mcp_url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, token)
    assert status == 200, status
    tool_names = [t["name"] for t in listing["result"]["tools"]]
    assert "echo" in tool_names, tool_names
    assert "ping" in tool_names, tool_names
    assert "secret_tool" not in tool_names, tool_names
    status, call = post(
        mcp_url,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"value": 1}},
        },
        token,
    )
    assert status == 200, status
    assert call["result"]["isError"] is False, call
    tool_text = call["result"]["content"][0]["text"]

    # (c) an ungranted tool is denied with isError=true, not a crash
    status, denied = post(
        mcp_url,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "secret_tool", "arguments": {}},
        },
        token,
    )
    assert status == 200, status
    assert denied["result"]["isError"] is True, denied
    print("DENIED_OK")

    # (d) surface the mediated model and tool answers on stdout
    print(model_text)
    print(tool_text)


main()
"""


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
    log: list[dict[str, object]],
    log_lock: threading.Lock,
) -> None:
    """Stand in for the Worker side of the wire: answer frames, record requests."""

    def send(message_type: str, request_id: str, payload: dict[str, object]) -> None:
        write_frame(write_stream, _frame(message_type, request_id, payload))

    # Grant the run's tools through a real initialize frame on the wire.
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
                },
                {
                    "name": PING_TOOL,
                    "description": "Ping tool",
                    "input_schema": {"type": "object", "properties": {}},
                },
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
                {
                    "output_text": MODEL_ANSWER,
                    "stop_reason": "stop",
                    "usage": {"input_tokens": 3, "output_tokens": 3, "total_tokens": 6},
                },
            )
        elif message_type == "tool_request":
            tool_payload = cast("dict[str, object]", payload)
            name = tool_payload.get("name")
            text = TOOL_ECHO_ANSWER if name == ECHO_TOOL else f"tool {name} invoked"
            send(
                "tool_response",
                request_id,
                {"content": {"text": text}, "is_error": False, "metadata": {}},
            )


@pytest.mark.anyio
async def test_real_shim_with_real_child_over_real_sockets() -> None:
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
        target=_fake_worker, args=(worker_read, worker_write, log, log_lock), daemon=True
    )
    worker_thread.start()

    # The initialize frame must be consumed before the child can list tools.
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
        CHILD_SCRIPT,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    assert process.stdin is not None
    process.stdin.write(b'{"input": "triage this"}\n')
    await process.stdin.drain()
    process.stdin.close()
    stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=90.0)

    try:
        assert process.returncode == 0, stderr_bytes.decode("utf-8", errors="replace")
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        assert "MODEL_OK" in stdout, stdout
        assert "TOOL_OK" in stdout, stdout
        assert AUTH_MARKER in stdout, stdout
        assert DENIED_MARKER in stdout, stdout

        with log_lock:
            wire_frames = list(log)
        model_requests = [f for f in wire_frames if f.get("type") == "model_request"]
        tool_names_on_wire: list[str] = []
        for frame in wire_frames:
            if frame.get("type") != "tool_request":
                continue
            payload = frame.get("payload")
            if isinstance(payload, dict):
                payload_item = cast("dict[str, object]", payload)
                name = payload_item.get("name")
                if isinstance(name, str):
                    tool_names_on_wire.append(name)
        assert len(model_requests) == 1, wire_frames
        assert ECHO_TOOL in tool_names_on_wire, tool_names_on_wire
        assert UNGRANTED_TOOL not in tool_names_on_wire, tool_names_on_wire
    finally:
        await server.stop()
        # Closing the write ends unblocks the blocked readers with EOF.
        worker_write.close()
        wire_in.close()
        wire_out.close()
        worker_thread.join(timeout=10.0)
        worker_read.close()
