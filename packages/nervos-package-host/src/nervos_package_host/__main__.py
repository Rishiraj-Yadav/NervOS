"""Run one isolated package-host health check or AgentEntrypoint execution."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import sys
from collections.abc import Mapping
from typing import BinaryIO, cast

from nervos_sdk import (
    SDK_API_VERSION,
    AgentContext,
    AgentResult,
    ModelRequest,
    ModelResult,
    ToolRequest,
    ToolResult,
)
from nervos_sdk.types import JSONValue

from nervos_package_host.runner import (
    HostEntrypointError,
    SerializedModelPort,
    SerializedToolPort,
    health_check,
    run_entrypoint,
)
from nervos_package_host.wire import (
    HOST_PROTOCOL_VERSION,
    HostProtocolError,
    read_frame,
    write_frame,
)


class _BoundedLog(io.TextIOBase):
    def __init__(self, limit: int = 16 * 1024) -> None:
        self._limit = limit
        self._seen = 0

    def writable(self) -> bool:
        return True

    def write(self, value: str) -> int:
        encoded = value.encode("utf-8", errors="replace")
        self._seen = min(self._limit, self._seen + len(encoded))
        return len(value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--health-check", action="store_true")
    parser.add_argument("--entrypoint")
    return parser.parse_args()


def _message(
    message_type: str, request_id: str, payload: Mapping[str, object]
) -> dict[str, object]:
    return {
        "protocol_version": HOST_PROTOCOL_VERSION,
        "type": message_type,
        "request_id": request_id,
        "payload": dict(payload),
    }


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise HostProtocolError(f"{field} must be an object")
    return cast("dict[str, object]", value)


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise HostProtocolError(f"{field} must be a string")
    return value


def _result_payload(result: AgentResult) -> dict[str, object]:
    return {
        "final_message": result.final_message,
        "output": dict(result.output),
        "metadata": dict(result.metadata),
    }


async def _run_session(protocol_in: BinaryIO, protocol_out: BinaryIO, entrypoint: str) -> None:
    """Run one entrypoint with worker-mediated ports on the canonical framed wire."""
    next_request = 0

    def rpc(message_type: str, payload: Mapping[str, object]) -> dict[str, object]:
        nonlocal next_request
        next_request += 1
        request_id = f"port-{next_request}"
        write_frame(protocol_out, _message(message_type, request_id, payload))
        response = read_frame(protocol_in)
        expected = "model_response" if message_type == "model_request" else "tool_response"
        if response["type"] != expected or response["request_id"] != request_id:
            raise HostProtocolError("port response does not match request")
        return _mapping(response["payload"], "port response")

    async def model_complete(request: ModelRequest) -> ModelResult:
        response = rpc(
            "model_request",
            {
                "messages": [
                    {"role": item.role, "content": item.content} for item in request.messages
                ],
                "model": request.model,
                "temperature": request.temperature,
                "max_output_tokens": request.max_output_tokens,
                "metadata": dict(request.metadata),
            },
        )
        stop_reason = response.get("stop_reason")
        return ModelResult(
            output_text=_string(response.get("output_text"), "model response output_text"),
            stop_reason=stop_reason if isinstance(stop_reason, str) else None,
            usage=cast("dict[str, int]", response.get("usage", {})),
        )

    async def tool_invoke(request: ToolRequest) -> ToolResult:
        response = rpc(
            "tool_request",
            {
                "name": request.name,
                "arguments": dict(request.arguments),
                "metadata": dict(request.metadata),
            },
        )
        return ToolResult(
            content=cast("dict[str, JSONValue]", response.get("content", {})),
            is_error=bool(response.get("is_error", False)),
            metadata=cast("dict[str, JSONValue]", response.get("metadata", {})),
        )

    request = read_frame(protocol_in)
    if request["type"] != "run_request":
        raise HostProtocolError("host expected run_request")
    payload = _mapping(request["payload"], "run request")
    configuration = _mapping(payload.get("configuration", {}), "configuration")
    context = payload.get("context")
    if context is not None and not isinstance(context, str):
        raise HostProtocolError("context must be a string or null")
    input_text = _string(payload.get("input_text"), "input_text")
    agent_context = AgentContext(
        run_id=_string(payload.get("run_id"), "run_id"),
        agent_instance_id=_string(payload.get("agent_instance_id"), "agent_instance_id"),
        configuration=cast("dict[str, JSONValue]", configuration),
        context=context,
        model=SerializedModelPort(model_complete),
        tools=SerializedToolPort(tool_invoke),
        input={"text": input_text},
    )
    result = await run_entrypoint(entrypoint, agent_context)
    write_frame(
        protocol_out,
        _message("run_result", str(request["request_id"]), _result_payload(result)),
    )


def main() -> int:
    protocol_out = sys.stdout.buffer
    protocol_in = sys.stdin.buffer
    sys.stdout = _BoundedLog()
    sys.stderr = _BoundedLog()
    args = parse_args()
    try:
        write_frame(
            protocol_out,
            _message(
                "host_hello",
                "hello",
                {
                    "host_protocol_version": HOST_PROTOCOL_VERSION,
                    "sdk_api_version": SDK_API_VERSION,
                },
            ),
        )
        if args.health_check:
            if not args.entrypoint:
                raise HostEntrypointError("health check requires entrypoint")
            health_check(args.entrypoint)
            write_frame(
                protocol_out,
                _message(
                    "ready",
                    "health",
                    {
                        "entrypoint": args.entrypoint,
                        "host_protocol_version": HOST_PROTOCOL_VERSION,
                        "sdk_api_version": SDK_API_VERSION,
                    },
                ),
            )
            return 0
        initialize = read_frame(protocol_in)
        if initialize["type"] != "initialize":
            raise HostProtocolError("host expected initialize")
        payload = cast("dict[str, object]", initialize["payload"])
        entrypoint = payload.get("entrypoint")
        if not isinstance(entrypoint, str):
            raise HostProtocolError("initialize requires entrypoint")
        health_check(entrypoint)
        write_frame(protocol_out, _message("ready", str(initialize["request_id"]), {}))
        asyncio.run(_run_session(protocol_in, protocol_out, entrypoint))
        return 0
    except (HostProtocolError, HostEntrypointError):
        with contextlib.suppress(Exception):
            write_frame(
                protocol_out, _message("run_error", "error", {"code": "package_host_error"})
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
