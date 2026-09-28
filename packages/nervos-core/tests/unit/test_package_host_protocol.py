"""Unit tests for G3 length-prefixed canonical JSON host wire protocol."""

# pyright: basic

from __future__ import annotations

import io

import pytest
from nervos_package_host.wire import (
    HOST_PROTOCOL_VERSION,
    MAX_FRAME_BYTES,
    HostProtocolError,
    read_frame,
    write_frame,
)


def test_valid_frame_roundtrip() -> None:
    message = {
        "protocol_version": HOST_PROTOCOL_VERSION,
        "type": "host_hello",
        "request_id": "req-1",
        "payload": {"status": "ok"},
    }
    buffer = io.BytesIO()
    write_frame(buffer, message)
    buffer.seek(0)
    parsed = read_frame(buffer)
    assert parsed == message


def test_frame_header_syntax() -> None:
    # Frame syntax is: <decimal length in bytes>\n<JSON bytes>
    message = {
        "protocol_version": HOST_PROTOCOL_VERSION,
        "type": "ready",
        "request_id": "req-2",
        "payload": {},
    }
    buffer = io.BytesIO()
    write_frame(buffer, message)
    raw = buffer.getvalue()
    header, _, body = raw.partition(b"\n")
    assert header.isdigit()
    assert len(body) == int(header)


def test_leading_zero_in_header_is_rejected() -> None:
    malformed = b'012\n{"protocol_version":"1","type":"ready","request_id":"1","payload":{}}'
    with pytest.raises(HostProtocolError):
        read_frame(io.BytesIO(malformed))


def test_non_digit_in_header_is_rejected() -> None:
    malformed = b'+12\n{"protocol_version":"1","type":"ready","request_id":"1","payload":{}}'
    with pytest.raises(HostProtocolError):
        read_frame(io.BytesIO(malformed))


def test_oversized_declared_length_is_rejected_before_read() -> None:
    oversized = f"{MAX_FRAME_BYTES + 1}\n".encode("ascii")
    with pytest.raises(HostProtocolError):
        read_frame(io.BytesIO(oversized))


def test_duplicate_json_keys_are_rejected() -> None:
    duplicate_payload = (
        b'{"protocol_version":"1","type":"ready","request_id":"1","payload":{},"payload":{}}'
    )
    frame = f"{len(duplicate_payload)}\n".encode("ascii") + duplicate_payload
    with pytest.raises(HostProtocolError):
        read_frame(io.BytesIO(frame))


def test_unknown_protocol_version_is_rejected() -> None:
    bad_version = {
        "protocol_version": "99",
        "type": "ready",
        "request_id": "req-1",
        "payload": {},
    }
    buffer = io.BytesIO()
    with pytest.raises(HostProtocolError):
        write_frame(buffer, bad_version)


def test_unknown_message_type_is_rejected() -> None:
    bad_type = {
        "protocol_version": HOST_PROTOCOL_VERSION,
        "type": "unknown_verb",
        "request_id": "req-1",
        "payload": {},
    }
    buffer = io.BytesIO()
    with pytest.raises(HostProtocolError):
        write_frame(buffer, bad_type)
