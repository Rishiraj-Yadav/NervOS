# Phase A1: launch the REAL package host (`python -m nervos_package_host`) as a
# subprocess, speak real wire frames to it, and let it run real children through
# the loopback shim. Nothing here mocks the host.

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

import pytest
from nervos_package_host.wire import (
    HOST_PROTOCOL_VERSION,
    canonical_payload,
    validate_message,
)

ECHO_TOOL = "echo"

CHILD = """
import json
import os
import sys
import urllib.request

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(url, body, token):
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json", "authorization": "Bearer " + token},
    )
    with opener.open(request, timeout=30) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def main():
    line = sys.stdin.readline()
    origin = os.environ["OPENAI_BASE_URL"][: -len("/v1")]
    assert os.environ["ANTHROPIC_BASE_URL"] == origin, os.environ
    assert os.environ["GOOGLE_GEMINI_BASE_URL"] == origin, os.environ
    key = os.environ["OPENAI_API_KEY"]
    assert os.environ["ANTHROPIC_API_KEY"] == key, os.environ
    assert os.environ["GEMINI_API_KEY"] == os.environ["GOOGLE_API_KEY"] == key
    assert "127.0.0.1" in origin, origin
    print("ENV_BASE_OK")

    status, completion = post(
        os.environ["OPENAI_BASE_URL"] + "/chat/completions",
        {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        key,
    )
    assert status == 200, status
    print(completion["choices"][0]["message"]["content"])

    status, call = post(
        os.environ["NERVOS_MCP_URL"],
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"value": 1}},
        },
        key,
    )
    assert status == 200, status
    assert call["result"]["isError"] is False, call
    print(call["result"]["content"][0]["text"])
    print("HOST_LAUNCH_OK")
    data = json.loads(line)
    print("STDIN:" + data["input"])


main()
"""

CRASH_CHILD = """
import sys

sys.exit(7)
"""

TREE_CHILD = """
import subprocess
import sys
import time
import os

pid_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "grandchild.pid")
grandchild = subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import os, sys, time\\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\\n"
        "time.sleep(120)\\n",
        pid_file,
    ]
)
print("TREE_PARENT_READY", flush=True)
time.sleep(120)
"""


def _message(message_type: str, request_id: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "protocol_version": HOST_PROTOCOL_VERSION,
        "type": message_type,
        "request_id": request_id,
        "payload": payload,
    }


async def _write_frame(writer: asyncio.StreamWriter, message: dict[str, object]) -> None:
    payload = canonical_payload(message)
    writer.write(str(len(payload)).encode("ascii") + b"\n" + payload)
    await writer.drain()


async def _read_frame(reader: asyncio.StreamReader) -> dict[str, object]:
    header = await reader.readline()
    if not header or not header.endswith(b"\n"):
        raise AssertionError("host closed the wire before a frame header")
    length = int(header.strip())
    payload = await reader.readexactly(length)
    document = json.loads(payload.decode("utf-8"))
    assert isinstance(document, dict)
    message = cast("dict[str, object]", document)
    validate_message(message)
    return message


async def _start_host(bundle: Path) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-m",
        "nervos_package_host",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=bundle,
        env=os.environ.copy(),
    )


async def _drive_host(
    bundle: Path,
    command: list[str],
    input_text: str,
    *,
    tools: list[dict[str, object]] | None = None,
    cancel_after_setup: CallableSetup | None = None,
) -> tuple[dict[str, object] | None, dict[str, object] | None, int, str]:
    """Run one real host session; return (result, error, host exit code, child stderr)."""
    host = await _start_host(bundle)
    stdin, stdout = host.stdin, host.stdout
    assert stdin is not None and stdout is not None
    hello = await asyncio.wait_for(_read_frame(stdout), timeout=30)
    assert hello["type"] == "host_hello", hello
    await _write_frame(
        stdin,
        _message(
            "initialize",
            "init",
            {
                "runtime_kind": "process",
                "command": command,
                "tools": tools if tools is not None else [],
                "package_root": str(bundle),
            },
        ),
    )
    ready = await asyncio.wait_for(_read_frame(stdout), timeout=30)
    assert ready["type"] == "ready", ready
    await _write_frame(stdin, _message("run_request", "run", {"input_text": input_text}))
    if cancel_after_setup is not None:
        await cancel_after_setup(bundle)
        await _write_frame(stdin, _message("cancel", "cancel", {}))

    result: dict[str, object] | None = None
    error: dict[str, object] | None = None
    while result is None and error is None:
        frame = await asyncio.wait_for(_read_frame(stdout), timeout=90)
        frame_type = frame["type"]
        if frame_type == "model_request":
            await _write_frame(
                stdin,
                _message(
                    "model_response",
                    str(frame["request_id"]),
                    {
                        "output_text": "HOST_MODEL_OK",
                        "stop_reason": "stop",
                        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    },
                ),
            )
        elif frame_type == "tool_request":
            payload = frame["payload"]
            assert isinstance(payload, dict)
            await _write_frame(
                stdin,
                _message(
                    "tool_response",
                    str(frame["request_id"]),
                    {"content": {"text": "TOOL_OK"}, "is_error": False},
                ),
            )
        elif frame_type == "run_result":
            result = cast("dict[str, object]", frame["payload"])
        elif frame_type == "run_error":
            error = cast("dict[str, object]", frame["payload"])
        else:
            raise AssertionError(f"unexpected frame {frame_type}")
    stderr_bytes = b""
    with_context = await asyncio.wait_for(host.communicate(), timeout=30)
    stderr_bytes = with_context[1]
    assert host.returncode is not None
    return result, error, host.returncode, stderr_bytes.decode("utf-8", errors="replace")


type CallableSetup = Callable[[Path], Awaitable[None]]


def _pid_alive(pid: int) -> bool:
    if os.name == "posix":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    listing = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, timeout=30
    )
    return f"{pid}" in listing.stdout


@pytest.mark.anyio
async def test_real_host_runs_process_child_end_to_end(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "child.py").write_text(CHILD, encoding="utf-8")

    result, error, code, stderr = await _drive_host(
        bundle,
        [sys.executable, "-u", "child.py"],
        "MARKER_INPUT",
        tools=[
            {
                "name": ECHO_TOOL,
                "description": "Echo tool",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
    )

    assert error is None, (error, stderr)
    assert code == 0, stderr
    assert result is not None
    final_message = result.get("final_message")
    assert isinstance(final_message, str), result
    # The child saw the bridge environment, got the run input on stdin, and its
    # mediated model + tool calls (answered by this test on the wire) succeeded;
    # its stdout became the run result.
    assert "ENV_BASE_OK" in final_message, final_message
    assert "STDIN:MARKER_INPUT" in final_message, final_message
    assert "HOST_MODEL_OK" in final_message, final_message
    assert "TOOL_OK" in final_message, final_message
    assert "HOST_LAUNCH_OK" in final_message, final_message


@pytest.mark.anyio
async def test_real_host_reports_run_error_for_failing_child(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "crash.py").write_text(CRASH_CHILD, encoding="utf-8")

    result, error, code, stderr = await _drive_host(
        bundle, [sys.executable, "-u", "crash.py"], "go"
    )

    assert result is None, result
    assert error is not None, (error, stderr)
    assert error.get("code") == "process_agent_failed", error
    assert code == 0, stderr


@pytest.mark.anyio
async def test_real_host_cancel_kills_the_child_tree(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "tree.py").write_text(TREE_CHILD, encoding="utf-8")
    pid_file = bundle / "grandchild.pid"

    async def wait_for_grandchild(bundle_root: Path) -> None:
        del bundle_root
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 20
        while not pid_file.exists():
            if loop.time() >= deadline:
                pytest.fail("grandchild never wrote its pid file")
            await asyncio.sleep(0.05)

    result, error, code, stderr = await _drive_host(
        bundle,
        [sys.executable, "-u", "tree.py"],
        "go",
        cancel_after_setup=wait_for_grandchild,
    )

    # The cancelled run closes as an error, not a result.
    assert result is None, (result, stderr)
    assert error is not None, (error, stderr)
    grandchild_pid = int(pid_file.read_text(encoding="utf-8").strip())
    deadline = asyncio.get_running_loop().time() + 20
    while _pid_alive(grandchild_pid):
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail(f"grandchild {grandchild_pid} survived the cancel")
        await asyncio.sleep(0.2)
    assert code == 0, stderr
