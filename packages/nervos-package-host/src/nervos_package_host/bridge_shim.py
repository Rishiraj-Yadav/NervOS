"""Loopback HTTP shim for generic process agents.

Provides OpenAI-compatible /v1/chat/completions and MCP /mcp endpoints that translate
to the existing wire protocol frames (model_request/tool_request) over stdio.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, BinaryIO, ClassVar, cast
from urllib.parse import parse_qs, urlparse

from nervos_package_host.wire import (
    HOST_PROTOCOL_VERSION,
    HostProtocolError,
    read_frame,
    write_frame,
)

_LOGGER = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 1024 * 1024
DEFAULT_REQUEST_TIMEOUT = 30.0
DEFAULT_MAX_REQUESTS = 1000


class ShimError(Exception):
    """Shim-specific error with HTTP status code."""

    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _finish_reason(stop_reason: object) -> str:
    """Map the wire stop reason onto the OpenAI chat finish_reason vocabulary."""
    if stop_reason == "tool_use":
        return "tool_calls"
    if stop_reason == "length":
        return "length"
    if stop_reason == "error":
        return "error"
    return "stop"


@dataclass(slots=True)
class ShimConfig:
    token: str
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    max_requests: int = DEFAULT_MAX_REQUESTS


def _new_pending() -> dict[str, asyncio.Future[dict[str, object]]]:
    return {}


def _new_tools_info() -> list[dict[str, object]]:
    return []


@dataclass(slots=True)
class ShimState:
    config: ShimConfig
    request_count: int = 0
    wire_in: BinaryIO | None = None
    wire_out: BinaryIO | None = None
    loop: asyncio.AbstractEventLoop | None = None
    next_request_id: int = 0
    pending: dict[str, asyncio.Future[dict[str, object]]] = field(default_factory=_new_pending)
    closed: bool = False
    tools_info: list[dict[str, object]] = field(default_factory=_new_tools_info)
    # The most recent model name the child asked for; discovery routes fall back to
    # it (or to "nervos-model") when the host was never told the run's model.
    last_model: str = ""

    def next_id(self) -> str:
        self.next_request_id += 1
        return f"shim-{self.next_request_id}"

    def check_request_budget(self) -> None:
        if self.request_count >= self.config.max_requests:
            raise ShimError("request limit exceeded", HTTPStatus.TOO_MANY_REQUESTS)
        self.request_count += 1


class _ShimRequestHandler(BaseHTTPRequestHandler):
    server_version = "NervOS-Shim/1.0"
    sys_version = ""
    state: ClassVar[ShimState | None] = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        _LOGGER.debug("%s - %s", self.address_string(), format % args)

    def _require_state(self) -> ShimState:
        state = self.state
        if state is None:
            raise ShimError("shim not initialized", HTTPStatus.SERVICE_UNAVAILABLE)
        return state

    def _new_future(self, state: ShimState) -> asyncio.Future[dict[str, object]]:
        # The HTTP server thread has no running event loop; futures are created on
        # the loop captured at start() and resolved by the wire reader on that loop.
        loop = state.loop
        if loop is None:
            raise ShimError("shim event loop not started", HTTPStatus.SERVICE_UNAVAILABLE)
        return loop.create_future()

    def _wait_for_response(self, state: ShimState, request_id: str, kind: str) -> dict[str, object]:
        """Block the HTTP server thread until the wire reader resolves the request."""
        future = state.pending[request_id]
        deadline = time.monotonic() + state.config.request_timeout
        while not future.done():
            if time.monotonic() >= deadline:
                state.pending.pop(request_id, None)
                raise ShimError(f"{kind} request timed out", HTTPStatus.GATEWAY_TIMEOUT)
            time.sleep(0.005)
        try:
            return future.result()
        except Exception as e:
            raise ShimError(f"{kind} request failed: {e}", HTTPStatus.BAD_GATEWAY) from e

    def _request_model(self, state: ShimState, payload: dict[str, object]) -> dict[str, object]:
        """Send one ``model_request`` frame and wait for its ``model_response``.

        This is the single canonical seam every vendor route shares: each route
        converts its request into the wire payload, this method does the framed
        round-trip, and the route shapes the returned payload its own way.
        """
        model = payload.get("model")
        if isinstance(model, str) and model:
            state.last_model = model
        request_id = state.next_id()
        future = self._new_future(state)
        state.pending[request_id] = future

        frame: dict[str, object] = {
            "protocol_version": HOST_PROTOCOL_VERSION,
            "type": "model_request",
            "request_id": request_id,
            "payload": payload,
        }

        wire_out = state.wire_out
        assert wire_out is not None
        write_frame(wire_out, frame)
        return self._wait_for_response(state, request_id, "model")

    def _require_auth(self, path: str = "") -> None:
        """Authorize one request: Bearer everywhere, plus the vendor key headers.

        ADR 0044: ``Authorization: Bearer`` is accepted on every route; the Anthropic
        route also accepts ``x-api-key`` and the Gemini routes also accept
        ``x-goog-api-key`` or ``?key=``, each carrying the same per-run key.
        """
        state = self._require_state()
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:] == state.config.token:
            return
        if path == "/v1/messages" and self.headers.get("x-api-key", "") == state.config.token:
            return
        if path.startswith("/v1beta/"):
            if self.headers.get("x-goog-api-key", "") == state.config.token:
                return
            query = parse_qs(urlparse(self.path).query)
            values = query.get("key", [])
            if values and values[0] == state.config.token:
                return
        if path == "/v1/messages":
            message = "invalid or missing x-api-key or bearer token"
        elif path.startswith("/v1beta/"):
            message = "invalid or missing api key"
        else:
            message = "invalid or missing bearer token"
        raise ShimError(message, HTTPStatus.UNAUTHORIZED)

    def _require_host(self) -> None:
        host = self.headers.get("Host", "")
        expected = f"127.0.0.1:{cast('HTTPServer', self.server).server_port}"
        if host != expected:
            raise ShimError(f"invalid Host header, expected {expected}", HTTPStatus.BAD_REQUEST)

    def _read_body(self) -> bytes:
        content_length = self.headers.get("Content-Length")
        if content_length is None:
            raise ShimError("missing Content-Length", HTTPStatus.LENGTH_REQUIRED)
        try:
            length = int(content_length)
        except ValueError as err:
            raise ShimError("invalid Content-Length", HTTPStatus.BAD_REQUEST) from err
        if length > MAX_REQUEST_BYTES:
            raise ShimError("request body too large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        body = self.rfile.read(length)
        if len(body) != length:
            raise ShimError("truncated request body", HTTPStatus.BAD_REQUEST)
        return body

    def _send_json(self, status: int, data: Mapping[str, object]) -> None:
        body = json.dumps(data, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, error: ShimError) -> None:
        self._send_json(error.status, {"error": {"message": error.message, "type": "shim_error"}})

    def _send_vendor_error(self, path: str, error: ShimError) -> None:
        """Errors carry the vendor's own error envelope for the route that failed."""
        if path == "/v1/messages":
            self._send_json(
                error.status,
                {
                    "type": "error",
                    "error": {"type": "invalid_request_error", "message": error.message},
                },
            )
        elif path.startswith("/v1beta/"):
            status = {
                400: "INVALID_ARGUMENT",
                401: "UNAUTHENTICATED",
                404: "NOT_FOUND",
                413: "INVALID_ARGUMENT",
                429: "RESOURCE_EXHAUSTED",
            }.get(error.status, "INTERNAL")
            self._send_json(
                error.status,
                {"error": {"code": error.status, "message": error.message, "status": status}},
            )
        else:
            self._send_error(error)

    def do_POST(self) -> None:
        state = self.state
        path = urlparse(self.path).path
        if state is None or state.closed:
            self._send_vendor_error(
                path, ShimError("shim not initialized", HTTPStatus.SERVICE_UNAVAILABLE)
            )
            return

        try:
            self._require_host()
            self._require_auth(path)
            state.check_request_budget()
            body = self._read_body()

            gemini = _gemini_route(path)
            if path == "/v1/chat/completions":
                self._handle_chat_completions(body)
            elif path == "/v1/messages":
                self._handle_anthropic_messages(body)
            elif path == "/v1/responses":
                self._handle_responses(body)
            elif gemini is not None:
                self._handle_gemini(gemini, body)
            elif path == "/mcp":
                self._handle_mcp(body)
            else:
                raise ShimError(f"unknown path: {path}", HTTPStatus.NOT_FOUND)
        except ShimError as e:
            self._send_vendor_error(path, e)
        except Exception:
            _LOGGER.exception("unhandled error in shim handler")
            self._send_vendor_error(
                path, ShimError("internal error", HTTPStatus.INTERNAL_SERVER_ERROR)
            )

    def do_GET(self) -> None:
        state = self.state
        path = urlparse(self.path).path
        if state is None or state.closed:
            self._send_vendor_error(
                path, ShimError("shim not initialized", HTTPStatus.SERVICE_UNAVAILABLE)
            )
            return
        try:
            self._require_host()
            self._require_auth(path)
            state.check_request_budget()
            if path == "/v1/models":
                self._send_json(HTTPStatus.OK, _openai_models_document(state))
            elif path == "/v1beta/models":
                self._send_json(HTTPStatus.OK, _gemini_models_document(state))
            else:
                raise ShimError(f"unknown path: {path}", HTTPStatus.NOT_FOUND)
        except ShimError as e:
            self._send_vendor_error(path, e)
        except Exception:
            _LOGGER.exception("unhandled error in shim handler")
            self._send_vendor_error(
                path, ShimError("internal error", HTTPStatus.INTERNAL_SERVER_ERROR)
            )

    def _handle_chat_completions(self, body: bytes) -> None:
        state = self._require_state()
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as e:
            raise ShimError("invalid JSON", HTTPStatus.BAD_REQUEST) from e

        if not isinstance(parsed, dict):
            raise ShimError("request must be an object", HTTPStatus.BAD_REQUEST)
        request = cast("dict[str, object]", parsed)

        normalized_messages = self._parse_chat_messages(request)
        offered_tools, wire_tool_choice = self._parse_chat_tools(request)
        stream = request.get("stream") is True
        include_usage = self._stream_include_usage(request)

        model = request.get("model")
        if not isinstance(model, str) or not model:
            raise ShimError("model is required", HTTPStatus.BAD_REQUEST)

        temperature = request.get("temperature")
        if temperature is not None and not isinstance(temperature, (int, float)):
            raise ShimError("temperature must be a number", HTTPStatus.BAD_REQUEST)

        max_tokens = request.get("max_tokens")
        if max_tokens is not None and not isinstance(max_tokens, int):
            raise ShimError("max_tokens must be an integer", HTTPStatus.BAD_REQUEST)

        payload: dict[str, object] = {
            "messages": normalized_messages,
            "model": model,
            "temperature": temperature,
            "max_output_tokens": max_tokens,
            "metadata": {},
        }
        if offered_tools:
            payload["tools"] = offered_tools
        if wire_tool_choice is not None:
            payload["tool_choice"] = wire_tool_choice

        # ADR 0042 owner decision: no real token streaming. The full result is
        # awaited first, then replayed as valid SSE for unmodified SDK clients.
        response = self._request_model(state, payload)

        tool_calls = self._response_tool_calls(response)
        output_text = response.get("output_text", "")
        text = output_text if isinstance(output_text, str) else ""
        finish_reason = _finish_reason(response.get("stop_reason"))
        usage_raw = response.get("usage", {})
        usage = cast("dict[str, object]", usage_raw) if isinstance(usage_raw, dict) else {}
        usage_payload: dict[str, object] = {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }
        completion_id = f"chatcmpl-{secrets.token_hex(12)}"
        created = int(time.time())
        if stream:
            self._send_chat_stream(
                completion_id,
                created,
                model,
                text,
                tool_calls,
                finish_reason,
                usage_payload,
                include_usage,
            )
            return
        message: dict[str, object] = {"role": "assistant", "content": output_text}
        if tool_calls:
            message["tool_calls"] = [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {"name": call["name"], "arguments": call["arguments"]},
                }
                for call in tool_calls
            ]
        completion: dict[str, object] = {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": usage_payload,
        }
        self._send_json(HTTPStatus.OK, completion)

    def _parse_chat_messages(self, request: dict[str, object]) -> list[dict[str, object]]:
        messages = request.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ShimError("messages must be a non-empty array", HTTPStatus.BAD_REQUEST)
        normalized: list[dict[str, object]] = []
        for msg in cast("list[object]", messages):
            if not isinstance(msg, dict):
                raise ShimError("each message must be an object", HTTPStatus.BAD_REQUEST)
            item = cast("dict[str, object]", msg)
            role = item.get("role")
            if role not in ("system", "user", "assistant", "tool"):
                error = "message role must be system, user, assistant, or tool"
                raise ShimError(error, HTTPStatus.BAD_REQUEST)
            content = self._message_text(item.get("content"))
            entry: dict[str, object] = {"role": role, "content": content}
            if role == "assistant":
                wire_calls = self._parse_openai_tool_calls(item.get("tool_calls"))
                if wire_calls:
                    entry["tool_calls"] = wire_calls
            elif role == "tool":
                tool_call_id = item.get("tool_call_id")
                if not isinstance(tool_call_id, str) or not tool_call_id:
                    error = "tool messages require a tool_call_id"
                    raise ShimError(error, HTTPStatus.BAD_REQUEST)
                entry["tool_call_id"] = tool_call_id
            normalized.append(entry)
        return normalized

    def _message_text(self, content: object) -> str:
        if content is None:
            # OpenAI clients send null content alongside assistant tool_calls.
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts: list[str] = []
            for part in cast("list[object]", content):
                part_item = cast("dict[str, object]", part) if isinstance(part, dict) else None
                if part_item is None or part_item.get("type") != "text":
                    error = "only text content parts are supported"
                    raise ShimError(error, HTTPStatus.BAD_REQUEST)
                text = part_item.get("text")
                if not isinstance(text, str):
                    error = "only text content parts are supported"
                    raise ShimError(error, HTTPStatus.BAD_REQUEST)
                text_parts.append(text)
            return "".join(text_parts)
        error = "message content must be string or array of text parts"
        raise ShimError(error, HTTPStatus.BAD_REQUEST)

    def _parse_openai_tool_calls(self, raw: object) -> list[dict[str, object]]:
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise ShimError("tool_calls must be an array", HTTPStatus.BAD_REQUEST)
        calls: list[dict[str, object]] = []
        for entry in cast("list[object]", raw):
            if not isinstance(entry, dict):
                raise ShimError("each tool_call must be an object", HTTPStatus.BAD_REQUEST)
            item = cast("dict[str, object]", entry)
            if item.get("type") not in (None, "function"):
                error = "only function tool calls are supported"
                raise ShimError(error, HTTPStatus.BAD_REQUEST)
            function = item.get("function")
            function_item = (
                cast("dict[str, object]", function) if isinstance(function, dict) else item
            )
            call_id = item.get("id")
            name = function_item.get("name")
            arguments = function_item.get("arguments")
            if not isinstance(call_id, str) or not call_id:
                raise ShimError("tool_call id is required", HTTPStatus.BAD_REQUEST)
            if not isinstance(name, str) or not name:
                raise ShimError("tool_call name is required", HTTPStatus.BAD_REQUEST)
            if isinstance(arguments, Mapping):
                arguments_text = json.dumps(
                    dict(cast("Mapping[str, object]", arguments)),
                    sort_keys=True,
                    separators=(",", ":"),
                )
            elif isinstance(arguments, str):
                arguments_text = arguments
            elif arguments is None:
                arguments_text = "{}"
            else:
                raise ShimError("tool_call arguments must be JSON text", HTTPStatus.BAD_REQUEST)
            calls.append({"id": call_id, "name": name, "arguments": arguments_text})
        return calls

    def _parse_chat_tools(
        self, request: dict[str, object]
    ) -> tuple[list[dict[str, object]], dict[str, object] | None]:
        if request.get("functions") is not None:
            raise ShimError("functions are not supported; send tools", HTTPStatus.BAD_REQUEST)
        raw_tools = request.get("tools")
        if raw_tools is None:
            return [], None
        if not isinstance(raw_tools, list):
            raise ShimError("tools must be an array", HTTPStatus.BAD_REQUEST)
        tools: list[dict[str, object]] = []
        for entry in cast("list[object]", raw_tools):
            if not isinstance(entry, dict):
                raise ShimError("each tool must be an object", HTTPStatus.BAD_REQUEST)
            item = cast("dict[str, object]", entry)
            if item.get("type") not in (None, "function"):
                error = "only function tools are supported"
                raise ShimError(error, HTTPStatus.BAD_REQUEST)
            function = item.get("function")
            function_item = (
                cast("dict[str, object]", function) if isinstance(function, dict) else item
            )
            name = function_item.get("name")
            description = function_item.get("description")
            parameters = function_item.get("parameters")
            if not isinstance(name, str) or not name:
                raise ShimError("tool name is required", HTTPStatus.BAD_REQUEST)
            if description is not None and not isinstance(description, str):
                error = "tool description must be a string"
                raise ShimError(error, HTTPStatus.BAD_REQUEST)
            if parameters is not None and not isinstance(parameters, dict):
                error = "tool parameters must be a JSON schema object"
                raise ShimError(error, HTTPStatus.BAD_REQUEST)
            tool: dict[str, object] = {
                "name": name,
                "description": description if isinstance(description, str) else "",
            }
            if isinstance(parameters, dict):
                tool["parameters"] = parameters
            tools.append(tool)
        return _resolve_tool_choice(request.get("tool_choice"), tools, _openai_named_choice)

    def _stream_include_usage(self, request: dict[str, object]) -> bool:
        options = request.get("stream_options")
        if options is None:
            return False
        if not isinstance(options, dict):
            raise ShimError("stream_options must be an object", HTTPStatus.BAD_REQUEST)
        return cast("dict[str, object]", options).get("include_usage") is True

    def _response_tool_calls(self, response: dict[str, object]) -> list[dict[str, object]]:
        raw = response.get("tool_calls")
        if not isinstance(raw, list):
            return []
        calls: list[dict[str, object]] = []
        for entry in cast("list[object]", raw):
            if not isinstance(entry, dict):
                continue
            item = cast("dict[str, object]", entry)
            call_id = item.get("id")
            name = item.get("name")
            arguments = item.get("arguments")
            if isinstance(call_id, str) and isinstance(name, str):
                calls.append(
                    {
                        "id": call_id,
                        "name": name,
                        "arguments": arguments if isinstance(arguments, str) else "{}",
                    }
                )
        return calls

    def _send_chat_stream(
        self,
        completion_id: str,
        created: int,
        model: str,
        text: str,
        tool_calls: list[dict[str, object]],
        finish_reason: str,
        usage: dict[str, object],
        include_usage: bool,
    ) -> None:
        def encode(document: dict[str, object]) -> bytes:
            return b"data: " + json.dumps(document, separators=(",", ":")).encode("utf-8") + b"\n\n"

        def chunk(delta: dict[str, object], finish: str | None) -> dict[str, object]:
            return {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }

        events = [encode(chunk({"role": "assistant", "content": ""}, None))]
        if text:
            events.append(encode(chunk({"content": text}, None)))
        for index, call in enumerate(tool_calls):
            call_id = cast("str", call["id"])
            name = cast("str", call["name"])
            arguments = cast("str", call["arguments"])
            events.append(
                encode(
                    chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": index,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {"name": name, "arguments": ""},
                                }
                            ]
                        },
                        None,
                    )
                )
            )
            events.append(
                encode(
                    chunk(
                        {"tool_calls": [{"index": index, "function": {"arguments": arguments}}]},
                        None,
                    )
                )
            )
        events.append(encode(chunk({}, finish_reason)))
        if include_usage:
            usage_document: dict[str, object] = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [],
                "usage": usage,
            }
            events.append(encode(usage_document))
        events.append(b"data: [DONE]\n\n")
        body = b"".join(events)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _parse_json_object(self, body: bytes) -> dict[str, object]:
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as e:
            raise ShimError("invalid JSON", HTTPStatus.BAD_REQUEST) from e
        if not isinstance(parsed, dict):
            raise ShimError("request must be an object", HTTPStatus.BAD_REQUEST)
        return cast("dict[str, object]", parsed)

    def _temperature_of(self, request: dict[str, object], key: str = "temperature") -> object:
        temperature = request.get(key)
        if temperature is not None and not isinstance(temperature, (int, float)):
            raise ShimError("temperature must be a number", HTTPStatus.BAD_REQUEST)
        return temperature

    def _model_result_parts(
        self, response: dict[str, object]
    ) -> tuple[str, list[dict[str, object]], object, dict[str, object]]:
        """The canonical result every route shapes its own way."""
        text_value = response.get("output_text", "")
        text = text_value if isinstance(text_value, str) else ""
        return (
            text,
            self._response_tool_calls(response),
            response.get("stop_reason"),
            cast(
                "dict[str, object]",
                response.get("usage", {}) if isinstance(response.get("usage"), dict) else {},
            ),
        )

    def _send_sse(self, events: list[tuple[str | None, object]]) -> None:
        parts: list[bytes] = []
        for event_name, document in events:
            chunk = b""
            if event_name is not None:
                chunk += b"event: " + event_name.encode("ascii") + b"\n"
            data = (
                document
                if isinstance(document, bytes)
                else json.dumps(document, separators=(",", ":")).encode("utf-8")
            )
            chunk += b"data: " + data + b"\n\n"
            parts.append(chunk)
        body = b"".join(parts)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------
    # Anthropic Messages: POST /v1/messages (ADR 0044)
    # ------------------------------------------------------------------

    def _handle_anthropic_messages(self, body: bytes) -> None:
        state = self._require_state()
        request = self._parse_json_object(body)
        version = self.headers.get("anthropic-version")
        if version is None or not version.strip():
            raise ShimError("anthropic-version header is required", HTTPStatus.BAD_REQUEST)

        messages = self._parse_anthropic_messages(request)
        offered_tools, wire_tool_choice = self._parse_anthropic_tools(request)
        model = request.get("model")
        if not isinstance(model, str) or not model:
            raise ShimError("model is required", HTTPStatus.BAD_REQUEST)
        max_tokens = request.get("max_tokens")
        if not isinstance(max_tokens, int) or max_tokens < 1:
            raise ShimError("max_tokens must be a positive integer", HTTPStatus.BAD_REQUEST)

        payload: dict[str, object] = {
            "messages": messages,
            "model": model,
            "temperature": self._temperature_of(request),
            "max_output_tokens": max_tokens,
            "metadata": {},
        }
        if offered_tools:
            payload["tools"] = offered_tools
        if wire_tool_choice is not None:
            payload["tool_choice"] = wire_tool_choice

        response = self._request_model(state, payload)
        text, tool_calls, stop_reason, usage = self._model_result_parts(response)
        content: list[dict[str, object]] = []
        if text:
            content.append({"type": "text", "text": text})
        for call in tool_calls:
            content.append(
                {
                    "type": "tool_use",
                    "id": call["id"],
                    "name": call["name"],
                    "input": _json_object(cast("str", call["arguments"])),
                }
            )
        anthropic_stop = "end_turn"
        if stop_reason == "tool_use":
            anthropic_stop = "tool_use"
        elif stop_reason == "length":
            anthropic_stop = "max_tokens"
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        message: dict[str, object] = {
            "id": f"msg_{secrets.token_hex(12)}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content if content else [{"type": "text", "text": ""}],
            "stop_reason": anthropic_stop,
            "stop_sequence": None,
            "usage": {
                "input_tokens": input_tokens if isinstance(input_tokens, int) else 0,
                "output_tokens": output_tokens if isinstance(output_tokens, int) else 0,
            },
        }
        if request.get("stream") is True:
            self._send_anthropic_stream(message, text, tool_calls, anthropic_stop, usage)
            return
        self._send_json(HTTPStatus.OK, message)

    def _parse_anthropic_messages(self, request: dict[str, object]) -> list[dict[str, object]]:
        raw = request.get("messages")
        if not isinstance(raw, list) or not raw:
            raise ShimError("messages must be a non-empty array", HTTPStatus.BAD_REQUEST)
        messages: list[dict[str, object]] = []
        system_text = _anthropic_system_text(request.get("system"))
        if system_text:
            messages.append({"role": "system", "content": system_text})
        for entry in cast("list[object]", raw):
            if not isinstance(entry, dict):
                raise ShimError("each message must be an object", HTTPStatus.BAD_REQUEST)
            item = cast("dict[str, object]", entry)
            role = item.get("role")
            if role not in ("user", "assistant"):
                raise ShimError("message role must be user or assistant", HTTPStatus.BAD_REQUEST)
            text_parts: list[str] = []
            tool_calls: list[dict[str, object]] = []
            tool_results: list[tuple[str, str]] = []
            content = item.get("content")
            blocks = cast("list[object]", content) if isinstance(content, list) else None
            if blocks is None:
                if isinstance(content, str):
                    text_parts.append(content)
                elif content is not None:
                    raise ShimError(
                        "content must be a string or block array", HTTPStatus.BAD_REQUEST
                    )
            else:
                for block in blocks:
                    if not isinstance(block, dict):
                        raise ShimError(
                            "each content block must be an object", HTTPStatus.BAD_REQUEST
                        )
                    block_item = cast("dict[str, object]", block)
                    block_type = block_item.get("type")
                    if block_type == "text":
                        block_text = block_item.get("text")
                        if not isinstance(block_text, str):
                            raise ShimError(
                                "text blocks require string text", HTTPStatus.BAD_REQUEST
                            )
                        text_parts.append(block_text)
                    elif block_type == "tool_use":
                        block_id = block_item.get("id")
                        name = block_item.get("name")
                        tool_input = block_item.get("input")
                        if not isinstance(block_id, str) or not block_id:
                            raise ShimError("tool_use blocks require an id", HTTPStatus.BAD_REQUEST)
                        if not isinstance(name, str) or not name:
                            raise ShimError(
                                "tool_use blocks require a name", HTTPStatus.BAD_REQUEST
                            )
                        if tool_input is not None and not isinstance(tool_input, dict):
                            raise ShimError(
                                "tool_use input must be an object", HTTPStatus.BAD_REQUEST
                            )
                        arguments = (
                            json.dumps(
                                cast("dict[str, object]", tool_input),
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                            if isinstance(tool_input, dict)
                            else "{}"
                        )
                        tool_calls.append({"id": block_id, "name": name, "arguments": arguments})
                    elif block_type == "tool_result":
                        tool_use_id = block_item.get("tool_use_id")
                        if not isinstance(tool_use_id, str) or not tool_use_id:
                            raise ShimError(
                                "tool_result blocks require a tool_use_id", HTTPStatus.BAD_REQUEST
                            )
                        result_content = block_item.get("content")
                        if isinstance(result_content, str):
                            result_text = result_content
                        elif isinstance(result_content, list):
                            result_text = _anthropic_text_of_blocks(
                                cast("list[object]", result_content)
                            )
                        elif result_content is None:
                            result_text = ""
                        else:
                            raise ShimError(
                                "tool_result content must be a string or text blocks",
                                HTTPStatus.BAD_REQUEST,
                            )
                        tool_results.append((tool_use_id, result_text))
                    else:
                        raise ShimError(
                            "only text, tool_use and tool_result blocks are supported",
                            HTTPStatus.BAD_REQUEST,
                        )
            if role == "assistant":
                assistant: dict[str, object] = {"role": "assistant", "content": "".join(text_parts)}
                if tool_calls:
                    assistant["tool_calls"] = tool_calls
                messages.append(assistant)
            elif tool_results:
                for index, (tool_use_id, result_text) in enumerate(tool_results):
                    combined = result_text
                    if text_parts and index == len(tool_results) - 1:
                        combined = (
                            result_text + "\n" + "".join(text_parts)
                            if result_text
                            else "".join(text_parts)
                        )
                    messages.append(
                        {"role": "tool", "content": combined, "tool_call_id": tool_use_id}
                    )
            else:
                messages.append({"role": "user", "content": "".join(text_parts)})
        return messages

    def _parse_anthropic_tools(
        self, request: dict[str, object]
    ) -> tuple[list[dict[str, object]], dict[str, object] | None]:
        raw_tools = request.get("tools")
        if raw_tools is None:
            return [], None
        if not isinstance(raw_tools, list):
            raise ShimError("tools must be an array", HTTPStatus.BAD_REQUEST)
        tools: list[dict[str, object]] = []
        for entry in cast("list[object]", raw_tools):
            if not isinstance(entry, dict):
                raise ShimError("each tool must be an object", HTTPStatus.BAD_REQUEST)
            item = cast("dict[str, object]", entry)
            name = item.get("name")
            description = item.get("description")
            input_schema = item.get("input_schema")
            if not isinstance(name, str) or not name:
                raise ShimError("tool name is required", HTTPStatus.BAD_REQUEST)
            if description is not None and not isinstance(description, str):
                raise ShimError("tool description must be a string", HTTPStatus.BAD_REQUEST)
            if input_schema is not None and not isinstance(input_schema, dict):
                raise ShimError("tool input_schema must be an object", HTTPStatus.BAD_REQUEST)
            tool: dict[str, object] = {
                "name": name,
                "description": description if isinstance(description, str) else "",
            }
            if isinstance(input_schema, dict):
                tool["parameters"] = input_schema
            tools.append(tool)
        return _resolve_tool_choice(
            _anthropic_choice_form(request.get("tool_choice")), tools, _flat_named_choice
        )

    def _send_anthropic_stream(
        self,
        message: dict[str, object],
        text: str,
        tool_calls: list[dict[str, object]],
        stop_reason: str,
        usage: dict[str, object],
    ) -> None:
        events: list[tuple[str | None, object]] = [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {**message, "content": [], "stop_reason": None},
                },
            )
        ]
        index = 0
        if text or not tool_calls:
            events.append(
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
            )
            events.append(
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "text_delta", "text": text},
                    },
                )
            )
            events.append(("content_block_stop", {"type": "content_block_stop", "index": index}))
            index += 1
        for call in tool_calls:
            events.append(
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {
                            "type": "tool_use",
                            "id": call["id"],
                            "name": call["name"],
                            "input": {},
                        },
                    },
                )
            )
            events.append(
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "input_json_delta", "partial_json": call["arguments"]},
                    },
                )
            )
            events.append(("content_block_stop", {"type": "content_block_stop", "index": index}))
            index += 1
        output_tokens = usage.get("output_tokens", 0)
        events.append(
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {
                        "output_tokens": output_tokens if isinstance(output_tokens, int) else 0
                    },
                },
            )
        )
        events.append(("message_stop", {"type": "message_stop"}))
        self._send_sse(events)

    # ------------------------------------------------------------------
    # OpenAI Responses: POST /v1/responses (ADR 0044)
    # ------------------------------------------------------------------

    def _handle_responses(self, body: bytes) -> None:
        state = self._require_state()
        request = self._parse_json_object(body)
        if request.get("previous_response_id") is not None:
            raise ShimError(
                "previous_response_id is not supported; send the full input each time",
                HTTPStatus.BAD_REQUEST,
            )
        # ``store`` is accepted and ignored: the shim is stateless per run.

        messages = self._parse_responses_input(request)
        offered_tools, wire_tool_choice = self._parse_responses_tools(request)
        model = request.get("model")
        if not isinstance(model, str) or not model:
            raise ShimError("model is required", HTTPStatus.BAD_REQUEST)
        max_tokens = request.get("max_output_tokens")
        if max_tokens is not None and not isinstance(max_tokens, int):
            raise ShimError("max_output_tokens must be an integer", HTTPStatus.BAD_REQUEST)

        payload: dict[str, object] = {
            "messages": messages,
            "model": model,
            "temperature": self._temperature_of(request),
            "max_output_tokens": max_tokens,
            "metadata": {},
        }
        if offered_tools:
            payload["tools"] = offered_tools
        if wire_tool_choice is not None:
            payload["tool_choice"] = wire_tool_choice

        response = self._request_model(state, payload)
        text, tool_calls, stop_reason, usage = self._model_result_parts(response)
        document = self._responses_document(model, text, tool_calls, stop_reason, usage)
        if request.get("stream") is True:
            self._send_responses_stream(document, text, tool_calls)
            return
        self._send_json(HTTPStatus.OK, document)

    def _parse_responses_input(self, request: dict[str, object]) -> list[dict[str, object]]:
        messages: list[dict[str, object]] = []
        instructions = request.get("instructions")
        if instructions is not None:
            if not isinstance(instructions, str):
                raise ShimError("instructions must be a string", HTTPStatus.BAD_REQUEST)
            if instructions:
                messages.append({"role": "system", "content": instructions})
        raw_input = request.get("input")
        if isinstance(raw_input, str):
            if not raw_input:
                raise ShimError("input must not be empty", HTTPStatus.BAD_REQUEST)
            messages.append({"role": "user", "content": raw_input})
            return messages
        if not isinstance(raw_input, list) or not raw_input:
            raise ShimError(
                "input must be a string or a non-empty item array", HTTPStatus.BAD_REQUEST
            )
        for entry in cast("list[object]", raw_input):
            if not isinstance(entry, dict):
                raise ShimError("each input item must be an object", HTTPStatus.BAD_REQUEST)
            item = cast("dict[str, object]", entry)
            item_type = item.get("type")
            if item_type == "reasoning":
                # Provider reasoning items carry no content the wire can represent;
                # they are accepted and dropped so echo-back clients keep working.
                continue
            if item_type == "message":
                role = item.get("role")
                if role not in ("user", "assistant", "system", "developer"):
                    raise ShimError("input message role is not supported", HTTPStatus.BAD_REQUEST)
                content = item.get("content")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = _responses_text_of_parts(cast("list[object]", content))
                else:
                    raise ShimError(
                        "input message content must be a string or content parts",
                        HTTPStatus.BAD_REQUEST,
                    )
                wire_role = "system" if role in ("system", "developer") else str(role)
                messages.append({"role": wire_role, "content": text})
            elif item_type == "function_call":
                call_id = item.get("call_id")
                name = item.get("name")
                arguments = item.get("arguments")
                if not isinstance(call_id, str) or not call_id:
                    raise ShimError("function_call items require call_id", HTTPStatus.BAD_REQUEST)
                if not isinstance(name, str) or not name:
                    raise ShimError("function_call items require a name", HTTPStatus.BAD_REQUEST)
                if not isinstance(arguments, str):
                    raise ShimError(
                        "function_call arguments must be JSON text", HTTPStatus.BAD_REQUEST
                    )
                messages.append(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"id": call_id, "name": name, "arguments": arguments}],
                    }
                )
            elif item_type == "function_call_output":
                call_id = item.get("call_id")
                output = item.get("output")
                if not isinstance(call_id, str) or not call_id:
                    raise ShimError(
                        "function_call_output items require call_id", HTTPStatus.BAD_REQUEST
                    )
                if isinstance(output, str):
                    output_text = output
                elif isinstance(output, list):
                    output_text = _responses_text_of_parts(cast("list[object]", output))
                elif output is None:
                    output_text = ""
                else:
                    raise ShimError(
                        "function_call_output must be a string or content parts",
                        HTTPStatus.BAD_REQUEST,
                    )
                messages.append({"role": "tool", "content": output_text, "tool_call_id": call_id})
            elif item_type is None:
                # The Vercel AI SDK sends plain {role, content} items without a type;
                # treat them exactly like typed message items (ADR 0045 real-SDK fix).
                role = item.get("role")
                if role not in ("user", "assistant", "system", "developer"):
                    raise ShimError("input item role is not supported", HTTPStatus.BAD_REQUEST)
                content = item.get("content")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = _responses_text_of_parts(cast("list[object]", content))
                else:
                    raise ShimError(
                        "input item content must be a string or content parts",
                        HTTPStatus.BAD_REQUEST,
                    )
                wire_role = "system" if role in ("system", "developer") else str(role)
                messages.append({"role": wire_role, "content": text})
            else:
                raise ShimError(
                    f"input item type '{item_type}' is not supported", HTTPStatus.BAD_REQUEST
                )
        return messages

    def _parse_responses_tools(
        self, request: dict[str, object]
    ) -> tuple[list[dict[str, object]], dict[str, object] | None]:
        raw_tools = request.get("tools")
        if raw_tools is None:
            return [], None
        if not isinstance(raw_tools, list):
            raise ShimError("tools must be an array", HTTPStatus.BAD_REQUEST)
        tools: list[dict[str, object]] = []
        for entry in cast("list[object]", raw_tools):
            if not isinstance(entry, dict):
                raise ShimError("each tool must be an object", HTTPStatus.BAD_REQUEST)
            item = cast("dict[str, object]", entry)
            if item.get("type") not in (None, "function"):
                raise ShimError(
                    f"tool type '{item.get('type')}' is not supported; only function tools",
                    HTTPStatus.BAD_REQUEST,
                )
            function = item.get("function")
            function_item = (
                cast("dict[str, object]", function) if isinstance(function, dict) else item
            )
            name = function_item.get("name")
            description = function_item.get("description")
            parameters = function_item.get("parameters")
            if not isinstance(name, str) or not name:
                raise ShimError("tool name is required", HTTPStatus.BAD_REQUEST)
            if description is not None and not isinstance(description, str):
                raise ShimError("tool description must be a string", HTTPStatus.BAD_REQUEST)
            if parameters is not None and not isinstance(parameters, dict):
                raise ShimError(
                    "tool parameters must be a JSON schema object", HTTPStatus.BAD_REQUEST
                )
            tool: dict[str, object] = {
                "name": name,
                "description": description if isinstance(description, str) else "",
            }
            if isinstance(parameters, dict):
                tool["parameters"] = parameters
            tools.append(tool)
        return _resolve_tool_choice(request.get("tool_choice"), tools, _flat_named_choice)

    def _responses_document(
        self,
        model: str,
        text: str,
        tool_calls: list[dict[str, object]],
        stop_reason: object,
        usage: dict[str, object],
    ) -> dict[str, object]:
        output: list[dict[str, object]] = []
        if text:
            output.append(
                {
                    "type": "message",
                    "id": f"msg_{secrets.token_hex(8)}",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                }
            )
        for call in tool_calls:
            output.append(
                {
                    "type": "function_call",
                    "id": f"fc_{secrets.token_hex(8)}",
                    "status": "completed",
                    "call_id": call["id"],
                    "name": call["name"],
                    "arguments": call["arguments"],
                }
            )
        status = "completed"
        document: dict[str, object] = {
            "id": f"resp_{secrets.token_hex(12)}",
            "object": "response",
            "created_at": int(time.time()),
            "status": status,
            "model": model,
            "output": output,
            "output_text": text,
            "usage": {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }
        if stop_reason == "length":
            document["status"] = "incomplete"
            document["incomplete_details"] = {"reason": "max_output_tokens"}
        return document

    def _send_responses_stream(
        self,
        document: dict[str, object],
        text: str,
        tool_calls: list[dict[str, object]],
    ) -> None:
        output = cast("list[dict[str, object]]", document["output"])
        events: list[tuple[str | None, object]] = [
            (
                "response.created",
                {
                    "type": "response.created",
                    "response": {**document, "output": [], "status": "in_progress"},
                },
            )
        ]
        for output_index, item in enumerate(output):
            item_id = cast("str", item.get("id", ""))
            added = {
                "type": "response.output_item.added",
                "output_index": output_index,
                "item": item,
            }
            events.append(("response.output_item.added", added))
            if item.get("type") == "message":
                events.append(
                    (
                        "response.output_text.delta",
                        {
                            "type": "response.output_text.delta",
                            "item_id": item_id,
                            "output_index": output_index,
                            "content_index": 0,
                            "delta": text,
                        },
                    )
                )
                events.append(
                    (
                        "response.output_text.done",
                        {
                            "type": "response.output_text.done",
                            "item_id": item_id,
                            "output_index": output_index,
                            "content_index": 0,
                            "text": text,
                        },
                    )
                )
            else:
                arguments = cast("str", item.get("arguments", ""))
                events.append(
                    (
                        "response.function_call_arguments.delta",
                        {
                            "type": "response.function_call_arguments.delta",
                            "item_id": item_id,
                            "output_index": output_index,
                            "delta": arguments,
                        },
                    )
                )
                events.append(
                    (
                        "response.function_call_arguments.done",
                        {
                            "type": "response.function_call_arguments.done",
                            "item_id": item_id,
                            "output_index": output_index,
                            "arguments": arguments,
                        },
                    )
                )
            events.append(
                (
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "output_index": output_index,
                        "item": item,
                    },
                )
            )
        events.append(("response.completed", {"type": "response.completed", "response": document}))
        self._send_sse(events)

    # ------------------------------------------------------------------
    # Gemini: POST /v1beta/models/{model}:generateContent and :streamGenerateContent
    # ------------------------------------------------------------------

    def _handle_gemini(self, route: tuple[str, bool], body: bytes) -> None:
        state = self._require_state()
        model_from_path, stream = route
        request = self._parse_json_object(body)
        alt_values = parse_qs(urlparse(self.path).query).get("alt", [])
        if alt_values and alt_values[0] != "sse":
            raise ShimError(
                f"alt={alt_values[0]} is not supported; use alt=sse", HTTPStatus.BAD_REQUEST
            )

        messages = self._parse_gemini_contents(request)
        offered_tools, wire_tool_choice = self._parse_gemini_tools(request)
        generation = request.get("generationConfig")
        generation_item = (
            cast("dict[str, object]", generation) if isinstance(generation, dict) else {}
        )
        temperature = generation_item.get("temperature")
        if temperature is not None and not isinstance(temperature, (int, float)):
            raise ShimError("generationConfig.temperature must be a number", HTTPStatus.BAD_REQUEST)
        max_tokens = generation_item.get("maxOutputTokens")
        if max_tokens is not None and not isinstance(max_tokens, int):
            raise ShimError(
                "generationConfig.maxOutputTokens must be an integer", HTTPStatus.BAD_REQUEST
            )

        payload: dict[str, object] = {
            "messages": messages,
            "model": model_from_path,
            "temperature": temperature,
            "max_output_tokens": max_tokens,
            "metadata": {},
        }
        if offered_tools:
            payload["tools"] = offered_tools
        if wire_tool_choice is not None:
            payload["tool_choice"] = wire_tool_choice

        response = self._request_model(state, payload)
        text, tool_calls, stop_reason, usage = self._model_result_parts(response)
        if stream:
            self._send_gemini_stream(model_from_path, text, tool_calls, stop_reason, usage)
            return
        self._send_json(
            HTTPStatus.OK, _gemini_document(model_from_path, text, tool_calls, stop_reason, usage)
        )

    def _parse_gemini_contents(self, request: dict[str, object]) -> list[dict[str, object]]:
        messages: list[dict[str, object]] = []
        system_instruction = request.get("systemInstruction")
        if system_instruction is not None:
            if isinstance(system_instruction, str):
                system_text = system_instruction
            elif isinstance(system_instruction, dict):
                parts = cast("dict[str, object]", system_instruction).get("parts")
                if not isinstance(parts, list):
                    raise ShimError(
                        "systemInstruction.parts must be an array", HTTPStatus.BAD_REQUEST
                    )
                system_text = _gemini_text_of_parts(cast("list[object]", parts))
            else:
                raise ShimError(
                    "systemInstruction must be a string or a parts object", HTTPStatus.BAD_REQUEST
                )
            if system_text:
                messages.append({"role": "system", "content": system_text})
        raw_contents = request.get("contents")
        if not isinstance(raw_contents, list) or not raw_contents:
            raise ShimError("contents must be a non-empty array", HTTPStatus.BAD_REQUEST)
        for entry in cast("list[object]", raw_contents):
            if not isinstance(entry, dict):
                raise ShimError("each content must be an object", HTTPStatus.BAD_REQUEST)
            item = cast("dict[str, object]", entry)
            role = item.get("role")
            if role not in ("user", "model", ""):
                raise ShimError("content role must be user or model", HTTPStatus.BAD_REQUEST)
            parts = item.get("parts")
            if not isinstance(parts, list) or not parts:
                raise ShimError("content parts must be a non-empty array", HTTPStatus.BAD_REQUEST)
            text_parts: list[str] = []
            tool_calls: list[dict[str, object]] = []
            tool_results: list[tuple[str, str]] = []
            for part in cast("list[object]", parts):
                if not isinstance(part, dict):
                    raise ShimError("each part must be an object", HTTPStatus.BAD_REQUEST)
                part_item = cast("dict[str, object]", part)
                if isinstance(part_item.get("text"), str):
                    text_parts.append(cast("str", part_item["text"]))
                elif isinstance(part_item.get("functionCall"), dict):
                    function_call = cast("dict[str, object]", part_item["functionCall"])
                    name = function_call.get("name")
                    args = function_call.get("args")
                    if not isinstance(name, str) or not name:
                        raise ShimError("functionCall requires a name", HTTPStatus.BAD_REQUEST)
                    if args is not None and not isinstance(args, dict):
                        raise ShimError(
                            "functionCall args must be an object", HTTPStatus.BAD_REQUEST
                        )
                    # Gemini addresses calls by name; the wire needs an id, so the
                    # name doubles as the call id the functionResponse refers back to.
                    arguments = (
                        json.dumps(
                            cast("dict[str, object]", args), sort_keys=True, separators=(",", ":")
                        )
                        if isinstance(args, dict)
                        else "{}"
                    )
                    tool_calls.append({"id": name, "name": name, "arguments": arguments})
                elif isinstance(part_item.get("functionResponse"), dict):
                    function_response = cast("dict[str, object]", part_item["functionResponse"])
                    name = function_response.get("name")
                    response_value = function_response.get("response")
                    if not isinstance(name, str) or not name:
                        raise ShimError("functionResponse requires a name", HTTPStatus.BAD_REQUEST)
                    if response_value is not None and not isinstance(response_value, dict):
                        raise ShimError(
                            "functionResponse response must be an object", HTTPStatus.BAD_REQUEST
                        )
                    response_text = (
                        json.dumps(
                            cast("dict[str, object]", response_value),
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        if isinstance(response_value, dict)
                        else "{}"
                    )
                    tool_results.append((name, response_text))
                else:
                    raise ShimError(
                        "only text, functionCall and functionResponse parts are supported",
                        HTTPStatus.BAD_REQUEST,
                    )
            if role == "model":
                assistant: dict[str, object] = {"role": "assistant", "content": "".join(text_parts)}
                if tool_calls:
                    assistant["tool_calls"] = tool_calls
                messages.append(assistant)
            elif tool_results:
                for name, response_text in tool_results:
                    messages.append(
                        {"role": "tool", "content": response_text, "tool_call_id": name}
                    )
                if text_parts:
                    raise ShimError(
                        "text parts alongside functionResponse are not supported",
                        HTTPStatus.BAD_REQUEST,
                    )
            else:
                messages.append({"role": "user", "content": "".join(text_parts)})
        return messages

    def _parse_gemini_tools(
        self, request: dict[str, object]
    ) -> tuple[list[dict[str, object]], dict[str, object] | None]:
        raw_tools = request.get("tools")
        if raw_tools is None:
            return [], None
        if not isinstance(raw_tools, list):
            raise ShimError("tools must be an array", HTTPStatus.BAD_REQUEST)
        tools: list[dict[str, object]] = []
        for entry in cast("list[object]", raw_tools):
            if not isinstance(entry, dict):
                raise ShimError("each tool must be an object", HTTPStatus.BAD_REQUEST)
            item = cast("dict[str, object]", entry)
            declarations = item.get("functionDeclarations")
            if not isinstance(declarations, list):
                raise ShimError(
                    "only functionDeclarations tools are supported", HTTPStatus.BAD_REQUEST
                )
            for declaration in cast("list[object]", declarations):
                if not isinstance(declaration, dict):
                    raise ShimError(
                        "each functionDeclaration must be an object", HTTPStatus.BAD_REQUEST
                    )
                declaration_item = cast("dict[str, object]", declaration)
                name = declaration_item.get("name")
                description = declaration_item.get("description")
                parameters = declaration_item.get("parameters")
                if not isinstance(name, str) or not name:
                    raise ShimError("functionDeclaration name is required", HTTPStatus.BAD_REQUEST)
                if description is not None and not isinstance(description, str):
                    raise ShimError(
                        "functionDeclaration description must be a string", HTTPStatus.BAD_REQUEST
                    )
                if parameters is not None and not isinstance(parameters, dict):
                    raise ShimError(
                        "functionDeclaration parameters must be a JSON schema object",
                        HTTPStatus.BAD_REQUEST,
                    )
                tool: dict[str, object] = {
                    "name": name,
                    "description": description if isinstance(description, str) else "",
                }
                if isinstance(parameters, dict):
                    tool["parameters"] = parameters
                tools.append(tool)
        return _resolve_tool_choice(
            _gemini_choice_form(request.get("toolConfig")), tools, _flat_named_choice
        )

    def _send_gemini_stream(
        self,
        model: str,
        text: str,
        tool_calls: list[dict[str, object]],
        stop_reason: object,
        usage: dict[str, object],
    ) -> None:
        events: list[tuple[str | None, object]] = []
        if text or not tool_calls:
            events.append(
                (
                    None,
                    {
                        "candidates": [
                            {
                                "content": {"role": "model", "parts": [{"text": text}]},
                                "index": 0,
                            }
                        ]
                    },
                )
            )
        for call in tool_calls:
            events.append(
                (
                    None,
                    {
                        "candidates": [
                            {
                                "content": {
                                    "role": "model",
                                    "parts": [
                                        {
                                            "functionCall": {
                                                "name": call["name"],
                                                "args": _json_object(
                                                    cast("str", call["arguments"])
                                                ),
                                            }
                                        }
                                    ],
                                },
                                "index": 0,
                            }
                        ]
                    },
                )
            )
        # The closing chunk carries only the finish reason and usage, never a
        # repeat of the content parts earlier chunks already delivered.
        finish_reason = "MAX_TOKENS" if stop_reason == "length" else "STOP"
        empty_parts: list[dict[str, object]] = []
        events.append(
            (
                None,
                {
                    "candidates": [
                        {
                            "content": {"role": "model", "parts": empty_parts},
                            "finishReason": finish_reason,
                            "index": 0,
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": usage.get("input_tokens", 0),
                        "candidatesTokenCount": usage.get("output_tokens", 0),
                        "totalTokenCount": usage.get("total_tokens", 0),
                    },
                    "modelVersion": model,
                },
            )
        )
        self._send_sse(events)

    def _handle_mcp(self, body: bytes) -> None:
        state = self._require_state()
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as e:
            raise ShimError("invalid JSON", HTTPStatus.BAD_REQUEST) from e

        if not isinstance(parsed, dict):
            raise ShimError("request must be an object", HTTPStatus.BAD_REQUEST)
        request = cast("dict[str, object]", parsed)

        jsonrpc = request.get("jsonrpc")
        if jsonrpc != "2.0":
            raise ShimError("jsonrpc must be '2.0'", HTTPStatus.BAD_REQUEST)

        method = request.get("method")
        if not isinstance(method, str):
            raise ShimError("method must be a string", HTTPStatus.BAD_REQUEST)

        params_raw = request.get("params", {})
        if not isinstance(params_raw, dict):
            raise ShimError("params must be an object", HTTPStatus.BAD_REQUEST)
        params = cast("dict[str, object]", params_raw)

        request_id = request.get("id")
        if request_id is not None and not isinstance(request_id, (str, int)):
            raise ShimError("id must be string, number, or null", HTTPStatus.BAD_REQUEST)

        is_notification = request_id is None

        if method == "initialize":
            result: dict[str, object] = {
                "protocolVersion": "2026-07-28",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "nervos-shim", "version": "1.0.0"},
            }
            jsonrpc_result = {"jsonrpc": "2.0", "id": request_id, "result": result}
            if not is_notification:
                self._send_json(HTTPStatus.OK, jsonrpc_result)
            else:
                self.send_response(HTTPStatus.ACCEPTED)
                self.end_headers()
            return

        if method == "notifications/initialized":
            self.send_response(HTTPStatus.ACCEPTED)
            self.end_headers()
            return

        if method == "ping":
            if not is_notification:
                self._send_json(HTTPStatus.OK, {"jsonrpc": "2.0", "id": request_id, "result": {}})
            else:
                self.send_response(HTTPStatus.ACCEPTED)
                self.end_headers()
            return

        if method == "tools/list":
            tools_info = self._get_tools_info()
            tools_list: list[dict[str, object]] = [
                {
                    "name": tool.get("name"),
                    "description": tool.get("description", f"MCP tool {tool.get('name')}"),
                    "inputSchema": tool.get("input_schema", {"type": "object", "properties": {}}),
                }
                for tool in tools_info
            ]
            list_result = {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools_list}}
            if not is_notification:
                self._send_json(HTTPStatus.OK, list_result)
            else:
                self.send_response(HTTPStatus.ACCEPTED)
                self.end_headers()
            return

        if method == "tools/call":
            tool_name = params.get("name")
            arguments_raw = params.get("arguments", {})
            if not isinstance(tool_name, str) or not tool_name:
                raise ShimError("tool name is required", HTTPStatus.BAD_REQUEST)
            if not isinstance(arguments_raw, dict):
                raise ShimError("arguments must be an object", HTTPStatus.BAD_REQUEST)
            arguments = cast("dict[str, object]", arguments_raw)

            granted = self._get_granted_tools()
            if tool_name not in granted:
                error_result: dict[str, object] = {
                    "isError": True,
                    "content": [{"type": "text", "text": f"tool '{tool_name}' is not granted"}],
                }
                denied_result = {"jsonrpc": "2.0", "id": request_id, "result": error_result}
                if not is_notification:
                    self._send_json(HTTPStatus.OK, denied_result)
                else:
                    self.send_response(HTTPStatus.ACCEPTED)
                    self.end_headers()
                return

            shim_request_id = state.next_id()
            future = self._new_future(state)
            state.pending[shim_request_id] = future

            frame: dict[str, object] = {
                "protocol_version": HOST_PROTOCOL_VERSION,
                "type": "tool_request",
                "request_id": shim_request_id,
                "payload": {"name": tool_name, "arguments": arguments, "metadata": {}},
            }

            wire_out = state.wire_out
            assert wire_out is not None
            write_frame(wire_out, frame)

            response = self._wait_for_response(state, shim_request_id, "tool")

            is_error = response.get("is_error", False)
            content_raw = response.get("content", {})
            content: dict[str, object] = (
                cast("dict[str, object]", content_raw) if isinstance(content_raw, dict) else {}
            )
            text_value = content.get("text", "")
            text = text_value if isinstance(text_value, str) else str(text_value)

            call_result: dict[str, object] = {
                "isError": is_error,
                "content": [{"type": "text", "text": text}],
            }
            jsonrpc_call_result = {"jsonrpc": "2.0", "id": request_id, "result": call_result}
            if not is_notification:
                self._send_json(HTTPStatus.OK, jsonrpc_call_result)
            else:
                self.send_response(HTTPStatus.ACCEPTED)
                self.end_headers()
            return

        raise ShimError(f"unknown method: {method}", HTTPStatus.METHOD_NOT_ALLOWED)

    def _get_granted_tools(self) -> list[str]:
        state = self._require_state()
        granted: list[str] = []
        for tool in state.tools_info:
            name = tool.get("name")
            if isinstance(name, str):
                granted.append(name)
        return granted

    def _get_tools_info(self) -> list[dict[str, object]]:
        return self._require_state().tools_info


# ------------------------------------------------------------------
# Vendor-route module helpers (ADR 0044)
# ------------------------------------------------------------------


def _resolve_tool_choice(
    raw: object,
    tools: list[dict[str, object]],
    named_form: Callable[[str], dict[str, object]],
) -> tuple[list[dict[str, object]], dict[str, object] | None]:
    """ADR 0045: resolve one vendor tool_choice onto (offered tools, wire choice).

    auto/none keep the ADR 0042 semantics; required and named now forward a
    ``tool_choice`` payload the Worker validates against the declared tools. A
    named choice for a tool that was not offered is refused here (400).
    """
    if raw is None or raw == "auto":
        return tools, None
    if raw == "none":
        # "none" is representable: the offered tools simply never reach the model.
        return [], {"kind": "none"}
    if raw == "required":
        return tools, {"kind": "required"}
    if isinstance(raw, str):
        raise ShimError(f"tool_choice '{raw}' is not supported", HTTPStatus.BAD_REQUEST)
    if not isinstance(raw, dict):
        raise ShimError("tool_choice must be a string or an object", HTTPStatus.BAD_REQUEST)
    name_value = cast("dict[str, object]", raw).get("name")
    if not isinstance(name_value, str) or not name_value:
        raise ShimError("a named tool_choice requires a tool name", HTTPStatus.BAD_REQUEST)
    offered_names = {
        cast("str", tool["name"]) for tool in tools if isinstance(tool.get("name"), str)
    }
    if name_value not in offered_names:
        raise ShimError(
            f"tool_choice names '{name_value}', which was not offered", HTTPStatus.BAD_REQUEST
        )
    return tools, {"kind": "named", "name": name_value}


def _openai_named_choice(name: str) -> dict[str, object]:
    return {"type": "function", "name": name}


def _flat_named_choice(name: str) -> dict[str, object]:
    # The resolver only inspects the name; both flat and nested vendor forms
    # normalize to the same wire choice.
    choice: dict[str, object] = {"name": name}
    return choice


def _anthropic_choice_form(raw: object) -> object:
    """Normalize Anthropic's tool_choice object onto the resolver's vocabulary."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        choice_type = cast("dict[str, object]", raw).get("type")
        if choice_type == "auto":
            return "auto"
        if choice_type == "none":
            return "none"
        if choice_type == "any":
            return "required"
        if choice_type == "tool":
            named: dict[str, object] = dict(cast("dict[str, object]", raw))
            return named
        raise ShimError(
            f"tool_choice type '{choice_type}' is not supported", HTTPStatus.BAD_REQUEST
        )
    raise ShimError("tool_choice must be an object with a type", HTTPStatus.BAD_REQUEST)


def _gemini_choice_form(raw: object) -> object:
    """Normalize Gemini's toolConfig onto the resolver's vocabulary."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ShimError("toolConfig must be an object", HTTPStatus.BAD_REQUEST)
    config = cast("dict[str, object]", raw).get("functionCallingConfig")
    if config is None:
        return None
    if not isinstance(config, dict):
        raise ShimError("functionCallingConfig must be an object", HTTPStatus.BAD_REQUEST)
    item = cast("dict[str, object]", config)
    mode = item.get("mode")
    if mode in (None, "", "AUTO"):
        return "auto"
    if mode == "NONE":
        return "none"
    if mode == "ANY":
        names = item.get("allowedFunctionNames")
        if isinstance(names, list) and names:
            first = cast("list[object]", names)[0]
            if isinstance(first, str):
                if len(cast("list[object]", names)) > 1:
                    raise ShimError(
                        "toolConfig ANY with more than one allowed function is not supported",
                        HTTPStatus.BAD_REQUEST,
                    )
                return {"type": "tool", "name": first}
            raise ShimError("allowedFunctionNames must be strings", HTTPStatus.BAD_REQUEST)
        return "required"
    raise ShimError(f"toolConfig mode '{mode}' is not supported", HTTPStatus.BAD_REQUEST)


def _json_object(text: str) -> dict[str, object]:
    """Parse JSON object text, failing closed to an empty object."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return cast("dict[str, object]", parsed) if isinstance(parsed, dict) else {}


def _anthropic_system_text(system: object) -> str:
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return _anthropic_text_of_blocks(cast("list[object]", system))
    raise ShimError("system must be a string or an array of text blocks", HTTPStatus.BAD_REQUEST)


def _anthropic_text_of_blocks(blocks: list[object]) -> str:
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict):
            block_item = cast("dict[str, object]", block)
            if block_item.get("type") == "text" and isinstance(block_item.get("text"), str):
                parts.append(cast("str", block_item["text"]))
                continue
        raise ShimError("only text blocks are supported here", HTTPStatus.BAD_REQUEST)
    return "".join(parts)


def _responses_text_of_parts(parts: list[object]) -> str:
    texts: list[str] = []
    for part in parts:
        if isinstance(part, dict):
            part_item = cast("dict[str, object]", part)
            if part_item.get("type") in ("input_text", "output_text") and isinstance(
                part_item.get("text"), str
            ):
                texts.append(cast("str", part_item["text"]))
                continue
        raise ShimError(
            "only input_text and output_text content parts are supported", HTTPStatus.BAD_REQUEST
        )
    return "".join(texts)


def _gemini_text_of_parts(parts: list[object]) -> str:
    texts: list[str] = []
    for part in parts:
        if isinstance(part, dict) and isinstance(cast("dict[str, object]", part).get("text"), str):
            texts.append(cast("str", cast("dict[str, object]", part)["text"]))
        else:
            raise ShimError("systemInstruction parts must be text", HTTPStatus.BAD_REQUEST)
    return "".join(texts)


def _gemini_route(path: str) -> tuple[str, bool] | None:
    """(model, stream) when the path is one of the Gemini generate routes."""
    prefix = "/v1beta/models/"
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix) :]
    for suffix, stream in ((":streamGenerateContent", True), (":generateContent", False)):
        if rest.endswith(suffix):
            model = rest[: -len(suffix)]
            if model and "/" not in model:
                return model, stream
    return None


def _gemini_document(
    model: str,
    text: str,
    tool_calls: list[dict[str, object]],
    stop_reason: object,
    usage: dict[str, object],
) -> dict[str, object]:
    parts: list[dict[str, object]] = []
    if text:
        parts.append({"text": text})
    for call in tool_calls:
        parts.append(
            {
                "functionCall": {
                    "name": call["name"],
                    "args": _json_object(cast("str", call["arguments"])),
                }
            }
        )
    finish_reason = "STOP"
    if stop_reason == "length":
        finish_reason = "MAX_TOKENS"
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": parts},
                "finishReason": finish_reason,
                "index": 0,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": usage.get("input_tokens", 0),
            "candidatesTokenCount": usage.get("output_tokens", 0),
            "totalTokenCount": usage.get("total_tokens", 0),
        },
        "modelVersion": model,
    }


def _discovery_model_name(state: ShimState) -> str:
    # The host is never told the run's model on the wire, so discovery names the
    # model the child used most recently, falling back to a fixed placeholder.
    return state.last_model or "nervos-model"


def _openai_models_document(state: ShimState) -> dict[str, object]:
    model = _discovery_model_name(state)
    return {
        "object": "list",
        "data": [{"id": model, "object": "model", "created": 0, "owned_by": "nervos"}],
    }


def _gemini_models_document(state: ShimState) -> dict[str, object]:
    model = _discovery_model_name(state)
    return {
        "models": [
            {
                "name": f"models/{model}",
                "displayName": model,
                "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
            }
        ]
    }


class ShimServer:
    def __init__(
        self,
        config: ShimConfig,
        wire_in: BinaryIO,
        wire_out: BinaryIO,
        message_handler: Callable[[dict[str, object]], Awaitable[None]],
    ) -> None:
        self._config = config
        self._wire_in = wire_in
        self._wire_out = wire_out
        self._message_handler = message_handler
        self._server: HTTPServer | None = None
        self._state = ShimState(config=config, wire_in=wire_in, wire_out=wire_out)
        self._reader_task: asyncio.Task[None] | None = None
        self._serve_future: asyncio.Future[None] | None = None
        _ShimRequestHandler.state = self._state

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("server not started")
        return self._server.server_port

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def state(self) -> ShimState:
        return self._state

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._state.loop = loop
        self._server = HTTPServer(("127.0.0.1", 0), _ShimRequestHandler)
        self._reader_task = asyncio.create_task(self._read_loop())
        # serve_forever blocks, so it runs on an executor thread; stop() unblocks it
        # via HTTPServer.shutdown().
        self._serve_future = loop.run_in_executor(None, self._server.serve_forever)

    async def _read_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._state.closed:
            try:
                message = await loop.run_in_executor(None, read_frame, self._wire_in)
            except (HostProtocolError, EOFError, OSError) as e:
                _LOGGER.debug("wire read ended: %s", e)
                break
            self.handle_wire_message(message)
            await self._message_handler(message)

    def handle_wire_message(self, message: dict[str, object]) -> None:
        msg_type = message.get("type")
        request_id = message.get("request_id")
        if (
            msg_type in ("model_response", "tool_response")
            and isinstance(request_id, str)
            and request_id in self._state.pending
        ):
            future = self._state.pending.pop(request_id)
            if not future.done():
                future.set_result(cast("dict[str, object]", message.get("payload", {})))
        elif msg_type == "initialize":
            payload_raw = message.get("payload", {})
            if isinstance(payload_raw, dict):
                payload = cast("dict[str, object]", payload_raw)
                tools = payload.get("tools")
                if isinstance(tools, list):
                    self._state.tools_info = cast("list[dict[str, object]]", tools)

    async def stop(self) -> None:
        self._state.closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._serve_future is not None:
            with contextlib.suppress(Exception):
                await self._serve_future
        _ShimRequestHandler.state = None


async def run_shim(
    wire_in: BinaryIO,
    wire_out: BinaryIO,
    *,
    token: str | None = None,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    max_requests: int = DEFAULT_MAX_REQUESTS,
) -> tuple[str, int]:
    """Run the shim server and return (token, port).

    This is a convenience function that starts the shim and blocks until the wire
    connection closes or an error occurs.
    """
    if token is None:
        token = secrets.token_urlsafe(32)

    config = ShimConfig(token=token, request_timeout=request_timeout, max_requests=max_requests)
    state = ShimState(config=config, wire_in=wire_in, wire_out=wire_out)
    state.loop = asyncio.get_running_loop()
    _ShimRequestHandler.state = state

    server = HTTPServer(("127.0.0.1", 0), _ShimRequestHandler)
    port = server.server_port

    async def reader_loop() -> None:
        loop = asyncio.get_running_loop()
        while not state.closed:
            try:
                message = await loop.run_in_executor(None, read_frame, wire_in)
            except (HostProtocolError, EOFError, OSError):
                break
            msg_type = message.get("type")
            request_id = message.get("request_id")
            if (
                msg_type in ("model_response", "tool_response")
                and isinstance(request_id, str)
                and request_id in state.pending
            ):
                future = state.pending.pop(request_id)
                if not future.done():
                    future.set_result(cast("dict[str, object]", message.get("payload", {})))

    reader_task = asyncio.create_task(reader_loop())
    try:
        await asyncio.get_running_loop().run_in_executor(None, server.serve_forever)
    finally:
        state.closed = True
        reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader_task
        server.server_close()
        _ShimRequestHandler.state = None

    return token, port
