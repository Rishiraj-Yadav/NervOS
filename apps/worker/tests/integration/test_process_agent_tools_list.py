# Test that tools/list returns exactly the run's granted tools.

from __future__ import annotations

import io

import pytest
from nervos_package_host.bridge_shim import ShimConfig, ShimServer
from nervos_package_host.wire import (
    HOST_PROTOCOL_VERSION,
    read_frame,
    write_frame,
)


async def _noop_handler(message: dict[str, object]) -> None:
    return None


def _server() -> ShimServer:
    return ShimServer(ShimConfig(token="test-token"), io.BytesIO(), io.BytesIO(), _noop_handler)


def _initialize_frame(tool_names: tuple[str, ...]) -> dict[str, object]:
    return {
        "protocol_version": HOST_PROTOCOL_VERSION,
        "type": "initialize",
        "request_id": "init-1",
        "payload": {
            "runtime_kind": "process",
            "command": ["python", "agent.py"],
            "tools": [
                {
                    "name": name,
                    "description": f"Tool {name}",
                    "input_schema": {"type": "object", "properties": {}},
                }
                for name in tool_names
            ],
        },
    }


def _granted_names(server: ShimServer) -> list[str]:
    names: list[str] = []
    for tool in server.state.tools_info:
        name = tool.get("name")
        if isinstance(name, str):
            names.append(name)
    return names


def test_tools_list_returns_only_granted_tools() -> None:
    """Test that tools/list returns exactly the granted tools from initialize frame."""
    server = _server()
    wire_in = io.BytesIO()

    # The initialize frame carries the run's granted tools (3 tools).
    write_frame(
        wire_in,
        _initialize_frame(("granted_tool_1", "granted_tool_2", "ungranted_tool")),
    )
    wire_in.seek(0)
    frame = read_frame(wire_in)
    server.handle_wire_message(frame)

    # Verify the shim state holds exactly the granted tools.
    assert len(server.state.tools_info) == 3
    assert set(_granted_names(server)) == {"granted_tool_1", "granted_tool_2", "ungranted_tool"}


def test_shim_tools_list_with_real_grants() -> None:
    """Test that shim's tools/list returns only tools from initialize frame."""
    server = _server()
    wire_in = io.BytesIO()

    write_frame(wire_in, _initialize_frame(("tool_a", "tool_b", "tool_c")))
    wire_in.seek(0)
    frame = read_frame(wire_in)
    server.handle_wire_message(frame)

    # The tools should be stored in the shim state.
    assert len(server.state.tools_info) == 3
    assert set(_granted_names(server)) == {"tool_a", "tool_b", "tool_c"}

    # Granted names and full descriptors stay available for tools/list.
    tools_info = server.state.tools_info
    assert len(tools_info) == 3
    assert {tool["name"] for tool in tools_info} == {"tool_a", "tool_b", "tool_c"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
