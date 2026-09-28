"""G3 private Worker/package-host wire format: bounded length-prefixed canonical JSON."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import BinaryIO, cast

HOST_PROTOCOL_VERSION = "1"
MAX_FRAME_BYTES = 1024 * 1024
MAX_HEADER_BYTES = 8

MESSAGE_TYPES: frozenset[str] = frozenset(
    {
        "host_hello",
        "initialize",
        "ready",
        "run_request",
        "model_request",
        "model_response",
        "tool_request",
        "tool_response",
        "log_event",
        "run_result",
        "run_error",
        "cancel",
        "cancelled",
        "shutdown",
    }
)
_REQUIRED_FIELDS = frozenset({"protocol_version", "type", "request_id", "payload"})


class HostProtocolError(ValueError):
    """A host protocol frame is malformed, oversized, unsupported, or out of sequence."""


def canonical_payload(message: Mapping[str, object]) -> bytes:
    validate_message(message)
    try:
        return json.dumps(
            message,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise HostProtocolError("message payload is not canonical JSON data") from error


def write_frame(stream: BinaryIO, message: Mapping[str, object]) -> None:
    payload = canonical_payload(message)
    if not payload or len(payload) > MAX_FRAME_BYTES:
        raise HostProtocolError("message exceeds protocol frame bound")
    header = str(len(payload)).encode("ascii") + b"\n"
    if len(header) > MAX_HEADER_BYTES:
        raise HostProtocolError("message header exceeds protocol bound")
    stream.write(header)
    stream.write(payload)
    stream.flush()


def read_frame(stream: BinaryIO) -> dict[str, object]:
    header = stream.readline(MAX_HEADER_BYTES + 1)
    if not header or not header.endswith(b"\n") or len(header) > MAX_HEADER_BYTES:
        raise HostProtocolError("invalid or truncated frame header")
    digits = header[:-1]
    if (
        not digits
        or not digits.isdigit()
        or (len(digits) > 1 and digits.startswith(b"0"))
        or digits == b"0"
    ):
        raise HostProtocolError("frame length must be canonical positive ASCII decimal")
    length = int(digits)
    if length > MAX_FRAME_BYTES:
        raise HostProtocolError("declared frame exceeds protocol bound")
    payload = stream.read(length)
    if len(payload) != length:
        raise HostProtocolError("protocol frame ended before declared payload length")
    try:
        document = json.loads(payload.decode("utf-8"), object_pairs_hook=_no_duplicate_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HostProtocolError("frame payload must be strict UTF-8 JSON") from error
    if not isinstance(document, dict):
        raise HostProtocolError("frame JSON root must be an object")
    message = cast("dict[str, object]", document)
    validate_message(message)
    return message


def validate_message(message: Mapping[str, object]) -> None:
    if frozenset(message) != _REQUIRED_FIELDS:
        raise HostProtocolError("message has missing or unknown fields")
    if message.get("protocol_version") != HOST_PROTOCOL_VERSION:
        raise HostProtocolError("unsupported host protocol version")
    message_type = message.get("type")
    if not isinstance(message_type, str) or message_type not in MESSAGE_TYPES:
        raise HostProtocolError("unknown host protocol message type")
    request_id = message.get("request_id")
    if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
        raise HostProtocolError("invalid protocol request id")
    if not isinstance(message.get("payload"), dict):
        raise HostProtocolError("protocol payload must be an object")


def _no_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise HostProtocolError("duplicate JSON object key")
        result[key] = value
    return result
