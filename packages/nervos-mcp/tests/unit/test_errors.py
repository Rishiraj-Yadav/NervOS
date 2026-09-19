"""The closed failure vocabulary: bounded, static, and impossible to enrich with remote text.

Everything here is a property of the vocabulary itself rather than of any operation, so each test
constructs the error directly and observes what it can carry.
"""

from __future__ import annotations

import inspect

import pytest
from nervos_mcp.errors import (
    McpConfigurationError,
    McpDiscoveryError,
    McpError,
    McpErrorCode,
    McpProtocolError,
    McpToolError,
)

_MAX_CODE_CHARS = 64
_MAX_MESSAGE_CHARS = 512

ERROR_CODE_MEMBERS = list(McpErrorCode)
ERROR_TYPES = [
    McpError,
    McpConfigurationError,
    McpDiscoveryError,
    McpProtocolError,
    McpToolError,
]


def test_every_error_code_has_a_message() -> None:
    for code in ERROR_CODE_MEMBERS:
        error = McpError(code)
        assert error.message
        assert isinstance(error.message, str)


def test_every_message_fits_the_durable_bounds() -> None:
    for code in ERROR_CODE_MEMBERS:
        error = McpError(code)
        assert 1 <= len(code.value) <= _MAX_CODE_CHARS, code
        assert 1 <= len(error.message) <= _MAX_MESSAGE_CHARS, code


def test_an_error_exposes_its_code_and_message() -> None:
    error = McpError(McpErrorCode.CATALOG_INVALID)

    assert error.code is McpErrorCode.CATALOG_INVALID
    assert error.message == str(error)
    assert repr(error) == "McpError('mcp_catalog_invalid')"


def test_the_message_is_identical_for_every_instance_of_a_code() -> None:
    first = McpError(McpErrorCode.SERVER_UNAVAILABLE)
    second = McpToolError(McpErrorCode.SERVER_UNAVAILABLE)

    assert first.message == second.message


def test_no_error_type_accepts_a_message_argument() -> None:
    """There is no constructor parameter for remote text, on the base or any subclass."""
    for error_type in ERROR_TYPES:
        parameters = list(inspect.signature(error_type.__init__).parameters)
        assert parameters == ["self", "code"], error_type.__name__


def test_an_error_cannot_be_constructed_with_remote_text() -> None:
    with pytest.raises(TypeError):
        McpError(McpErrorCode.TOOL_FAILED, "remote server said: boom")  # pyright: ignore[reportCallIssue]
