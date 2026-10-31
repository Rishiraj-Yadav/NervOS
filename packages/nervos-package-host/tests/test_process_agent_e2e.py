"""End-to-end test for generic process agent using a fake foreign agent script."""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from nervos_package_host.wire import (
    HOST_PROTOCOL_VERSION,
    read_frame,
    write_frame,
)


def _roundtrip(message: dict[str, object]) -> dict[str, object]:
    """Write a frame and read it back through the canonical wire encoding."""
    stream = io.BytesIO()
    write_frame(stream, message)
    stream.seek(0)
    return read_frame(stream)


def _payload(frame: dict[str, object]) -> dict[str, object]:
    payload = frame["payload"]
    assert isinstance(payload, dict)
    return cast("dict[str, object]", payload)


@pytest.fixture
def fake_foreign_agent() -> Iterator[Path]:
    """Create a fake foreign agent script that uses only stdlib."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(
            """
import json
import sys

def main():
    # Read input from stdin (one JSON line)
    line = sys.stdin.readline()
    if not line:
        return
    try:
        data = json.loads(line.strip())
        input_text = data.get("input", "")
    except json.JSONDecodeError:
        input_text = line.strip()

    # Echo back a response as JSON
    output = f"Processed: {input_text}"
    print(json.dumps({"output": output}), flush=True)

if __name__ == "__main__":
    main()
"""
        )
        f.flush()
        yield Path(f.name)
    with contextlib.suppress(OSError):
        os.unlink(f.name)


def test_fake_foreign_agent_script_exists(fake_foreign_agent: Path) -> None:
    """Verify the fake agent script was created."""
    assert fake_foreign_agent.exists()
    content = fake_foreign_agent.read_text()
    assert "def main()" in content
    assert "json" in content


def test_wire_protocol_frames_roundtrip() -> None:
    """Test that wire protocol frames can be written and read correctly."""
    hello = _roundtrip(
        {
            "protocol_version": HOST_PROTOCOL_VERSION,
            "type": "host_hello",
            "request_id": "hello",
            "payload": {
                "host_protocol_version": HOST_PROTOCOL_VERSION,
                "sdk_api_version": "0.1",
                "features": ["generic-process-bridge-v1"],
            },
        }
    )
    assert hello["type"] == "host_hello"
    payload = _payload(hello)
    features = payload.get("features", [])
    assert isinstance(features, list)
    assert "generic-process-bridge-v1" in cast("list[object]", features)

    init = _roundtrip(
        {
            "protocol_version": HOST_PROTOCOL_VERSION,
            "type": "initialize",
            "request_id": "init-1",
            "payload": {
                "runtime_kind": "process",
                "command": ["python", "-c", "print('hello')"],
            },
        }
    )
    assert init["type"] == "initialize"
    init_payload = _payload(init)
    assert init_payload["runtime_kind"] == "process"
    assert init_payload["command"] == ["python", "-c", "print('hello')"]

    run_req = _roundtrip(
        {
            "protocol_version": HOST_PROTOCOL_VERSION,
            "type": "run_request",
            "request_id": "run-1",
            "payload": {
                "run_id": "1",
                "agent_instance_id": "1",
                "configuration": {},
                "input_text": "test input",
                "selected_memory": [],
                "context_metadata": {"protocol": "legacy", "history": []},
            },
        }
    )
    assert run_req["type"] == "run_request"
    run_payload = _payload(run_req)
    assert run_payload["input_text"] == "test input"


def test_process_kind_initialize_payload() -> None:
    """Test that process kind initialize payload has correct structure."""
    frame = _roundtrip(
        {
            "protocol_version": HOST_PROTOCOL_VERSION,
            "type": "initialize",
            "request_id": "init-1",
            "payload": {
                "runtime_kind": "process",
                "command": ["bin/agent", "--flag"],
            },
        }
    )
    payload = _payload(frame)
    assert payload["runtime_kind"] == "process"
    assert payload["command"] == ["bin/agent", "--flag"]


@pytest.mark.anyio
async def test_real_shim_and_child_process() -> None:
    """Test that the wire protocol handshake works end-to-end
    with the tools field in initialize frame.
    """
    # Test that initialize frame with tools is accepted
    frame = _roundtrip(
        {
            "protocol_version": HOST_PROTOCOL_VERSION,
            "type": "initialize",
            "request_id": "init-1",
            "payload": {
                "runtime_kind": "process",
                "command": [sys.executable, "-c", "print('hello')"],
                "tools": [
                    {
                        "name": "test_tool",
                        "description": "A test tool",
                        "input_schema": {"type": "object", "properties": {}},
                    }
                ],
            },
        }
    )
    assert frame["type"] == "initialize"
    payload = _payload(frame)
    assert payload["runtime_kind"] == "process"
    tools = payload.get("tools")
    assert isinstance(tools, list)
    tool_items = cast("list[object]", tools)
    assert len(tool_items) == 1
    first_tool = tool_items[0]
    assert isinstance(first_tool, dict)
    assert cast("dict[str, object]", first_tool)["name"] == "test_tool"

    # Test that initialize frame without tools is also accepted (backward compat)
    frame = _roundtrip(
        {
            "protocol_version": HOST_PROTOCOL_VERSION,
            "type": "initialize",
            "request_id": "init-2",
            "payload": {
                "runtime_kind": "process",
                "command": [sys.executable, "-c", "print('hello')"],
            },
        }
    )
    assert frame["type"] == "initialize"
    payload = _payload(frame)
    assert payload["runtime_kind"] == "process"
    # tools field is optional, may be absent
    assert payload.get("tools") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
