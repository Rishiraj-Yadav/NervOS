"""Smoke-test the loopback shim against the real vendor SDKs (ADR 0044/0041).

Starts a real ShimServer with a fake wire Worker, then runs SDK client scripts as
child processes under a throwaway interpreter (``--sdk-python``) that has the real
``openai``, ``anthropic`` and ``google-genai`` packages installed. The children use
each SDK with its default settings and only the environment the production host
sets, so any incompatibility is the shim's, not the client's.

No live provider call ever happens: every request stays on 127.0.0.1.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import BinaryIO, cast

from bridge_node_cases import (
    NODE_ANTHROPIC_CASE,
    NODE_GEMINI_CASE,
    NODE_OPENAI_CASE,
    NODE_VERCEL_CASE,
)
from nervos_package_host.__main__ import process_agent_environment
from nervos_package_host.bridge_shim import ShimConfig, ShimServer
from nervos_package_host.wire import (
    HOST_PROTOCOL_VERSION,
    HostProtocolError,
    read_frame,
    write_frame,
)

ECHO_TOOL = "echo"
PLAIN_TEXT = "SMOKE_OK"
TOOL_FINAL_TEXT = "TOOL_FINAL_OK"

OPENAI_CHILD = r"""
import importlib.metadata
import json
import os

print("VERSION openai|" + importlib.metadata.version("openai"))
from openai import OpenAI

client = OpenAI()  # OPENAI_BASE_URL / OPENAI_API_KEY come from the environment


def run(name, fn):
    try:
        fn()
        print(f"RESULT openai|{name}|PASS")
    except Exception as error:
        status = getattr(error, "status_code", None)
        body = getattr(error, "body", None) or getattr(error, "message", None) or ""
        print(f"RESULT openai|{name}|FAIL|{type(error).__name__}|{status}|{str(body)[:200]}")


def chat_plain():
    completion = client.chat.completions.create(
        model="smoke-model", messages=[{"role": "user", "content": "hi"}]
    )
    assert completion.choices[0].message.content == "SMOKE_OK", completion


def chat_stream():
    chunks = []
    for chunk in client.chat.completions.create(
        model="smoke-model",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
    ):
        if chunk.choices and chunk.choices[0].delta.content:
            chunks.append(chunk.choices[0].delta.content)
    assert "".join(chunks) == "SMOKE_OK", chunks


def chat_tool():
    tools = [{
        "type": "function",
        "function": {
            "name": "echo",
            "description": "Echo tool",
            "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}},
        },
    }]
    first = client.chat.completions.create(
        model="smoke-model",
        messages=[{"role": "user", "content": "run the echo tool"}],
        tools=tools,
    )
    call = first.choices[0].message.tool_calls[0]
    assert call.function.name == "echo", call
    assert json.loads(call.function.arguments) == {"value": 7}, call
    final = client.chat.completions.create(
        model="smoke-model",
        messages=[
            {"role": "user", "content": "run the echo tool"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": "echo", "arguments": call.function.arguments},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": call.id, "content": "TOOL_RESULT_OK"},
        ],
        tools=tools,
    )
    assert final.choices[0].message.content == "TOOL_FINAL_OK", final


def responses_plain():
    response = client.responses.create(model="smoke-model", input="hi")
    assert response.output_text == "SMOKE_OK", response


def responses_stream():
    chunks = []
    for event in client.responses.create(
        model="smoke-model", input="hi", stream=True
    ):
        if event.type == "response.output_text.delta":
            chunks.append(event.delta)
    assert "".join(chunks) == "SMOKE_OK", chunks


def responses_tool():
    tools = [{
        "type": "function",
        "name": "echo",
        "description": "Echo tool",
        "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}},
    }]
    first = client.responses.create(
        model="smoke-model", input="run the echo tool", tools=tools
    )
    call = [item for item in first.output if item.type == "function_call"][0]
    assert call.name == "echo", call
    assert json.loads(call.arguments) == {"value": 7}, call
    final = client.responses.create(
        model="smoke-model",
        input=[
            {"type": "message", "role": "user", "content": "run the echo tool"},
            {
                "type": "function_call",
                "call_id": call.call_id,
                "name": "echo",
                "arguments": call.arguments,
            },
            {
                "type": "function_call_output",
                "call_id": call.call_id,
                "output": "TOOL_RESULT_OK",
            },
        ],
        tools=tools,
    )
    assert final.output_text == "TOOL_FINAL_OK", final


def models_list():
    models = list(client.models.list())
    assert models, "no models listed"


for name, fn in [
    ("chat_plain", chat_plain),
    ("chat_stream", chat_stream),
    ("chat_tool", chat_tool),
    ("responses_plain", responses_plain),
    ("responses_stream", responses_stream),
    ("responses_tool", responses_tool),
    ("models_list", models_list),
]:
    run(name, fn)
"""

ANTHROPIC_CHILD = r"""
import importlib.metadata
import json
import os

print("VERSION anthropic|" + importlib.metadata.version("anthropic"))
from anthropic import Anthropic

client = Anthropic()  # ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY come from the environment


def run(name, fn):
    try:
        fn()
        print(f"RESULT anthropic|{name}|PASS")
    except Exception as error:
        status = getattr(error, "status_code", None)
        body = getattr(error, "body", None) or getattr(error, "message", None) or ""
        print(f"RESULT anthropic|{name}|FAIL|{type(error).__name__}|{status}|{str(body)[:200]}")


def messages_plain():
    message = client.messages.create(
        model="claude-smoke", max_tokens=64, messages=[{"role": "user", "content": "hi"}]
    )
    assert message.content[0].text == "SMOKE_OK", message
    assert message.stop_reason == "end_turn", message


def messages_stream():
    text = []
    with client.messages.stream(
        model="claude-smoke", max_tokens=64, messages=[{"role": "user", "content": "hi"}]
    ) as stream:
        for delta in stream.text_stream:
            text.append(delta)
    assert "".join(text) == "SMOKE_OK", text


def messages_tool():
    tools = [{
        "name": "echo",
        "description": "Echo tool",
        "input_schema": {"type": "object", "properties": {"value": {"type": "integer"}}},
    }]
    first = client.messages.create(
        model="claude-smoke",
        max_tokens=64,
        messages=[{"role": "user", "content": "run the echo tool"}],
        tools=tools,
    )
    block = [b for b in first.content if b.type == "tool_use"][0]
    assert block.name == "echo", block
    assert block.input == {"value": 7}, block
    final = client.messages.create(
        model="claude-smoke",
        max_tokens=64,
        messages=[
            {"role": "user", "content": "run the echo tool"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": block.id, "name": "echo", "input": {"value": 7}}
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": block.id, "content": "TOOL_RESULT_OK"}
                ],
            },
        ],
        tools=tools,
    )
    assert final.content[0].text == "TOOL_FINAL_OK", final


def models_list():
    page = client.models.list()
    assert page.data, "no models listed"


for name, fn in [
    ("messages_plain", messages_plain),
    ("messages_stream", messages_stream),
    ("messages_tool", messages_tool),
    ("models_list", models_list),
]:
    run(name, fn)
"""

GEMINI_CHILD = r"""
import importlib.metadata
import json
import os

print("VERSION google-genai|" + importlib.metadata.version("google-genai"))
from google import genai
from google.genai import types

client = genai.Client(
    http_options={"base_url": os.environ["GOOGLE_GEMINI_BASE_URL"]}
)  # GEMINI_API_KEY comes from the environment


def run(name, fn):
    try:
        fn()
        print(f"RESULT google-genai|{name}|PASS")
    except Exception as error:
        status = getattr(getattr(error, "response", None), "status_code", None)
        body = getattr(getattr(error, "response", None), "text", "") or str(error)
        print(f"RESULT google-genai|{name}|FAIL|{type(error).__name__}|{status}|{str(body)[:200]}")


def generate_plain():
    response = client.models.generate_content(model="gemini-smoke", contents="hi")
    assert response.text == "SMOKE_OK", response


def generate_stream():
    text = []
    for chunk in client.models.generate_content_stream(model="gemini-smoke", contents="hi"):
        if chunk.text:
            text.append(chunk.text)
    assert "".join(text) == "SMOKE_OK", text


def generate_tool():
    config = types.GenerateContentConfig(
        tools=[types.Tool(
            function_declarations=[types.FunctionDeclaration(
                name="echo",
                description="Echo tool",
                parameters={"type": "object", "properties": {"value": {"type": "integer"}}},
            )]
        )]
    )
    first = client.models.generate_content(
        model="gemini-smoke", contents="run the echo tool", config=config
    )
    part = [p for p in first.candidates[0].content.parts if p.function_call][0]
    assert part.function_call.name == "echo", part
    assert dict(part.function_call.args) == {"value": 7}, part
    final = client.models.generate_content(
        model="gemini-smoke",
        contents=[
            types.Content(role="user", parts=[types.Part(text="run the echo tool")]),
            types.Content(role="model", parts=[types.Part(
                function_call=types.FunctionCall(name="echo", args={"value": 7})
            )]),
            types.Content(role="user", parts=[types.Part(
                function_response=types.FunctionResponse(
                    name="echo", response={"output": "TOOL_RESULT_OK"}
                )
            )]),
        ],
        config=config,
    )
    assert final.text == "TOOL_FINAL_OK", final


for name, fn in [
    ("generate_plain", generate_plain),
    ("generate_stream", generate_stream),
    ("generate_tool", generate_tool),
]:
    run(name, fn)
"""

SDK_CHILDREN: tuple[tuple[str, str], ...] = (
    ("openai", OPENAI_CHILD),
    ("anthropic", ANTHROPIC_CHILD),
    ("google-genai", GEMINI_CHILD),
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
    log: list[dict[str, object]],
    log_lock: threading.Lock,
) -> None:
    """Grant the tool, then answer model_request frames with the canned outcomes."""

    def send(message_type: str, request_id: str, payload: dict[str, object]) -> None:
        write_frame(write_stream, _frame(message_type, request_id, payload))

    def route(payload: dict[str, object]) -> dict[str, object]:
        usage = {"input_tokens": 5, "output_tokens": 6, "total_tokens": 11}
        messages = payload.get("messages")
        roles: list[str] = []
        if isinstance(messages, list):
            for message in cast("list[object]", messages):
                if isinstance(message, dict):
                    role = cast("dict[str, object]", message).get("role")
                    if isinstance(role, str):
                        roles.append(role)
        if roles and roles[-1] == "tool":
            return {"output_text": TOOL_FINAL_TEXT, "stop_reason": "stop", "usage": usage}
        if payload.get("tools") is not None:
            # Honor a forced named choice: the "model" calls the tool it was told to call.
            choice = payload.get("tool_choice")
            forced_name = ECHO_TOOL
            if isinstance(choice, dict):
                item = cast("dict[str, object]", choice)
                if item.get("kind") == "named" and isinstance(item.get("name"), str):
                    forced_name = cast("str", item["name"])
            return {
                "output_text": "",
                "stop_reason": "tool_use",
                "usage": usage,
                "tool_calls": [{"id": "call-1", "name": forced_name, "arguments": '{"value": 7}'}],
            }
        return {"output_text": PLAIN_TEXT, "stop_reason": "stop", "usage": usage}

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
            send("model_response", request_id, route(cast("dict[str, object]", payload)))


def _child_environment(origin: str, token: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(process_agent_environment(origin, token))
    # A developer machine's proxy must not intercept the loopback traffic.
    proxy_variables = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    )
    for name in proxy_variables:
        env.pop(name, None)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    return env


async def _run_sdk_child(
    sdk_python: str,
    script: str,
    stdin_text: str,
    env: dict[str, str],
    *,
    node_script: str | None = None,
) -> tuple[int, str, str]:
    # The child runs through the asyncio subprocess API, never a blocking call:
    # the wire reader resolves shim futures on this loop while the child works.
    argv = [sdk_python, node_script] if node_script is not None else [sdk_python, "-c", script]
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=Path(node_script).resolve().parent if node_script else None,
    )
    stdout_bytes, stderr_bytes = await asyncio.wait_for(
        process.communicate(stdin_text.encode("utf-8")), timeout=120
    )
    assert process.returncode is not None
    return (
        process.returncode,
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
    )


async def _serve_and_run(sdk_python: str, node_dir: str | None) -> int:
    token = "smoke-" + secrets.token_hex(16)
    to_shim_read_fd, to_shim_write_fd = os.pipe()
    from_shim_read_fd, from_shim_write_fd = os.pipe()
    wire_in = cast("BinaryIO", os.fdopen(to_shim_read_fd, "rb", buffering=0))
    worker_write = cast("BinaryIO", os.fdopen(to_shim_write_fd, "wb", buffering=0))
    worker_read = cast("BinaryIO", os.fdopen(from_shim_read_fd, "rb", buffering=0))
    wire_out = cast("BinaryIO", os.fdopen(from_shim_write_fd, "wb", buffering=0))

    async def noop_handler(message: dict[str, object]) -> None:
        return None

    server = ShimServer(
        ShimConfig(token=token, request_timeout=30.0, max_requests=500),
        wire_in,
        wire_out,
        noop_handler,
    )
    server.start()
    origin = f"http://127.0.0.1:{server.port}"

    log: list[dict[str, object]] = []
    log_lock = threading.Lock()
    worker_thread = threading.Thread(
        target=_fake_worker, args=(worker_read, worker_write, log, log_lock), daemon=True
    )
    worker_thread.start()

    deadline = time.monotonic() + 5.0
    while not server.state.tools_info:
        if time.monotonic() >= deadline:
            print("FAIL setup: shim did not consume the initialize frame")
            return 1
        await asyncio.sleep(0.01)

    env = _child_environment(origin, token)
    failures = 0
    versions: dict[str, str] = {}
    try:
        for sdk_name, script in SDK_CHILDREN:
            code, stdout, stderr = await _run_sdk_child(
                sdk_python, script, '{"input": "run"}\n', env
            )
            failures += _report_child(sdk_name, code, stdout, stderr, versions)
        if node_dir is not None:
            node_bin = _node_bin()
            node_versions = _node_versions(Path(node_dir))
            versions.update(node_versions)
            for sdk_name, script in NODE_CASES:
                script_path = Path(node_dir) / f"_smoke_{sdk_name.replace('/', '_')}.mjs"
                script_path.write_text(script, encoding="utf-8")
                code, stdout, stderr = await _run_sdk_child(
                    node_bin,
                    "",
                    '{"input": "run"}\n',
                    env,
                    node_script=str(script_path),
                )
                failures += _report_child(sdk_name, code, stdout, stderr, versions)

    finally:
        await server.stop()
        worker_write.close()
        wire_in.close()
        wire_out.close()
        worker_thread.join(timeout=10.0)
        worker_read.close()
    if failures:
        with log_lock:
            print(f"--- {len(log)} wire frames crossed the shim ---")
            for message in log[-4:]:
                print(json.dumps(message, sort_keys=True, default=str)[:600])
    return 1 if failures else 0


NODE_CASES: tuple[tuple[str, str], ...] = (
    ("openai", NODE_OPENAI_CASE),
    ("anthropic", NODE_ANTHROPIC_CASE),
    ("google-genai", NODE_GEMINI_CASE),
    ("vercel-ai", NODE_VERCEL_CASE),
)


def _node_bin() -> str:
    import shutil

    node = shutil.which("node")
    if node is None:
        raise SystemExit("node is not installed; cannot run the Node SDK smoke cases")
    return node


def _node_versions(node_dir: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in ("openai", "@anthropic-ai/sdk", "@google/genai", "ai"):
        package_json = node_dir / "node_modules" / name / "package.json"
        if package_json.exists():
            data = json.loads(package_json.read_text(encoding="utf-8"))
            versions[name] = str(data.get("version", "?"))
    return versions


def _report_child(
    sdk_name: str,
    code: int,
    stdout: str,
    stderr: str,
    versions: dict[str, str],
) -> int:
    """Print PASS/FAIL lines for one child; return its failure count."""
    failures = 0
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("VERSION "):
            versions[sdk_name] = line[len("VERSION ") :].split("|", 1)[1]
        elif line.startswith("RESULT "):
            fields = line[len("RESULT ") :].split("|")
            version = versions.get(sdk_name, "?")
            if len(fields) >= 3 and fields[2] == "PASS":
                print(f"PASS {sdk_name}=={version} {fields[1]}")
            else:
                failures += 1
                detail = "|".join(fields[3:]) if len(fields) > 3 else "?"
                print(f"FAIL {sdk_name}=={version} {fields[1]} :: {detail}")
    if code != 0:
        failures += 1
        print(f"FAIL {sdk_name} interpreter exited {code}:")
        for line in stderr.strip().splitlines()[-8:]:
            print(f"  {line}")
    return failures


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sdk-python",
        required=True,
        help="path to the throwaway interpreter with openai/anthropic/google-genai installed",
    )
    parser.add_argument(
        "--node-dir",
        help=(
            "path to a throwaway directory with node_modules containing openai, "
            "@anthropic-ai/sdk, @google/genai, ai and @ai-sdk/openai"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return asyncio.run(_serve_and_run(args.sdk_python, args.node_dir))


if __name__ == "__main__":
    raise SystemExit(main())
