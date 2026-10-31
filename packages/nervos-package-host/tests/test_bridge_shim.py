"""Unit tests for the generic process agent bridge shim."""

from __future__ import annotations

import asyncio
import http.client
import io
import json
from typing import cast

import pytest
from nervos_package_host.bridge_shim import (
    MAX_REQUEST_BYTES,
    ShimConfig,
    ShimError,
    ShimServer,
    ShimState,
    run_shim,
)
from nervos_package_host.wire import (
    HOST_PROTOCOL_VERSION,
    canonical_payload,
    read_frame,
    write_frame,
)


async def _noop_handler(message: dict[str, object]) -> None:
    return None


def _post(
    port: int,
    path: str,
    body: bytes,
    *,
    token: str | None = "secret",
    host: str | None = None,
    content_length: str | None = None,
) -> tuple[int, bytes]:
    """POST to the loopback shim over a real socket and return (status, body)."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if host is not None:
        headers["Host"] = host
    if content_length is not None:
        headers["Content-Length"] = content_length
    connection.request("POST", path, body=body, headers=headers)
    response = connection.getresponse()
    payload = response.read()
    status = response.status
    connection.close()
    return status, payload


def _error_message(payload: bytes) -> str:
    document = json.loads(payload.decode("utf-8"))
    assert isinstance(document, dict)
    error = cast("dict[str, object]", document).get("error")
    assert isinstance(error, dict)
    message = cast("dict[str, object]", error).get("message")
    assert isinstance(message, str)
    return message


def test_shim_config_defaults() -> None:
    config = ShimConfig(token="test-token")
    assert config.token == "test-token"
    assert config.request_timeout == 30.0
    assert config.max_requests == 1000


def test_shim_state_request_budget() -> None:
    config = ShimConfig(token="test", max_requests=5)
    state = ShimState(config=config)
    for _ in range(5):
        state.check_request_budget()
    with pytest.raises(ShimError, match="request limit exceeded"):
        state.check_request_budget()


def test_shim_state_next_id() -> None:
    config = ShimConfig(token="test")
    state = ShimState(config=config)
    id1 = state.next_id()
    id2 = state.next_id()
    assert id1 == "shim-1"
    assert id2 == "shim-2"


class TestShimRequestValidation:
    """Request validation exercised over real loopback HTTP against a live shim."""

    @pytest.mark.anyio
    async def test_rejects_wrong_or_missing_bearer_token(self) -> None:
        server = ShimServer(ShimConfig(token="secret"), io.BytesIO(), io.BytesIO(), _noop_handler)
        server.start()
        try:
            status, payload = _post(server.port, "/v1/chat/completions", b"", token="wrong")
            assert status == 401
            assert "invalid or missing bearer token" in _error_message(payload)
            status, payload = _post(server.port, "/v1/chat/completions", b"", token=None)
            assert status == 401
        finally:
            await server.stop()

    @pytest.mark.anyio
    async def test_rejects_a_foreign_host_header(self) -> None:
        server = ShimServer(ShimConfig(token="secret"), io.BytesIO(), io.BytesIO(), _noop_handler)
        server.start()
        try:
            status, payload = _post(server.port, "/v1/chat/completions", b"", host="evil.example")
            assert status == 400
            assert "invalid Host header" in _error_message(payload)
        finally:
            await server.stop()

    @pytest.mark.anyio
    async def test_rejects_an_oversized_request_body(self) -> None:
        server = ShimServer(ShimConfig(token="secret"), io.BytesIO(), io.BytesIO(), _noop_handler)
        server.start()
        try:
            status, payload = _post(
                server.port,
                "/v1/chat/completions",
                b"",
                content_length=str(MAX_REQUEST_BYTES + 1),
            )
            assert status == 413
            assert "request body too large" in _error_message(payload)
        finally:
            await server.stop()


class TestShimWireProtocol:
    def test_model_request_response_roundtrip(self) -> None:
        wire_in = io.BytesIO()
        wire_out = io.BytesIO()
        server = ShimServer(ShimConfig(token="test-token"), wire_in, wire_out, _noop_handler)
        state = server.state

        request_id = state.next_id()
        loop = asyncio.new_event_loop()
        try:
            future: asyncio.Future[dict[str, object]] = loop.create_future()
            state.pending[request_id] = future

            response_frame: dict[str, object] = {
                "protocol_version": HOST_PROTOCOL_VERSION,
                "type": "model_response",
                "request_id": request_id,
                "payload": {
                    "output_text": "Hello, world!",
                    "stop_reason": "stop",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                    },
                },
            }
            write_frame(wire_in, response_frame)
            wire_in.seek(0)

            message = read_frame(wire_in)
            assert message["type"] == "model_response"
            assert message["request_id"] == request_id

            server.handle_wire_message(message)
            assert future.done()
            result = future.result()
            assert result["output_text"] == "Hello, world!"
        finally:
            loop.close()

    def test_tool_request_response_roundtrip(self) -> None:
        wire_in = io.BytesIO()
        wire_out = io.BytesIO()
        server = ShimServer(ShimConfig(token="test-token"), wire_in, wire_out, _noop_handler)
        state = server.state

        request_id = state.next_id()
        loop = asyncio.new_event_loop()
        try:
            future: asyncio.Future[dict[str, object]] = loop.create_future()
            state.pending[request_id] = future

            response_frame: dict[str, object] = {
                "protocol_version": HOST_PROTOCOL_VERSION,
                "type": "tool_response",
                "request_id": request_id,
                "payload": {
                    "content": {"text": "tool result"},
                    "is_error": False,
                    "metadata": {},
                },
            }
            write_frame(wire_in, response_frame)
            wire_in.seek(0)

            message = read_frame(wire_in)
            assert message["type"] == "tool_response"

            server.handle_wire_message(message)
            assert future.done()
            result = future.result()
            content = result["content"]
            assert isinstance(content, dict)
            assert content["text"] == "tool result"
        finally:
            loop.close()


class TestShimErrorMapping:
    def test_shim_error_status_codes(self) -> None:
        err = ShimError("bad request", 400)
        assert err.status == 400

        err = ShimError("unauthorized", 401)
        assert err.status == 401

        err = ShimError("not found", 404)
        assert err.status == 404

        err = ShimError("too many", 429)
        assert err.status == 429


class TestShimServer:
    def test_shim_server_creation(self) -> None:
        server = ShimServer(
            ShimConfig(token="test-token"), io.BytesIO(), io.BytesIO(), _noop_handler
        )
        assert server.state.config.token == "test-token"

    def test_handle_wire_message_initialize_with_tools(self) -> None:
        """The shim stores granted tools from an initialize frame."""
        wire_in = io.BytesIO()
        wire_out = io.BytesIO()
        server = ShimServer(ShimConfig(token="test-token"), wire_in, wire_out, _noop_handler)

        init_frame: dict[str, object] = {
            "protocol_version": HOST_PROTOCOL_VERSION,
            "type": "initialize",
            "request_id": "init-1",
            "payload": {
                "runtime_kind": "process",
                "command": ["python", "agent.py"],
                "tools": [
                    {
                        "name": "my_tool",
                        "description": "A test tool",
                        "input_schema": {
                            "type": "object",
                            "properties": {"arg": {"type": "string"}},
                        },
                    }
                ],
            },
        }
        write_frame(wire_in, init_frame)
        wire_in.seek(0)
        frame = read_frame(wire_in)
        server.handle_wire_message(frame)
        tools_info = server.state.tools_info
        assert len(tools_info) == 1
        assert tools_info[0]["name"] == "my_tool"
        assert tools_info[0]["description"] == "A test tool"
        input_schema = tools_info[0]["input_schema"]
        assert isinstance(input_schema, dict)
        assert input_schema["type"] == "object"

    def test_get_granted_tools_from_tools_info(self) -> None:
        """Granted tool names derive from the stored tools_info."""
        server = ShimServer(
            ShimConfig(token="test-token"), io.BytesIO(), io.BytesIO(), _noop_handler
        )
        server.state.tools_info = [
            {"name": "tool1", "description": "Tool 1", "input_schema": {}},
            {"name": "tool2", "description": "Tool 2", "input_schema": {}},
        ]

        granted = [tool["name"] for tool in server.state.tools_info]
        assert granted == ["tool1", "tool2"]

    def test_get_tools_info(self) -> None:
        """The full tool descriptors stay available for tools/list."""
        server = ShimServer(
            ShimConfig(token="test-token"), io.BytesIO(), io.BytesIO(), _noop_handler
        )
        server.state.tools_info = [
            {"name": "tool1", "description": "Tool 1", "input_schema": {"type": "object"}},
        ]

        tools_info = server.state.tools_info
        assert len(tools_info) == 1
        assert tools_info[0]["name"] == "tool1"
        assert tools_info[0]["description"] == "Tool 1"


def test_run_shim_imports() -> None:
    assert callable(run_shim)
    assert canonical_payload is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
