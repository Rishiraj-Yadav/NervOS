"""The closed MCP failure vocabulary, and the one rule that shapes it.

Everything here exists to make a single property structural rather than a matter of discipline:
**a durable or public MCP failure carries a fixed, NervOS-authored sentence and nothing else.** No
remote protocol revision, no hostname, no server exception text, no stderr, no SDK message, no URL,
no secret ever reaches a persisted ``last_error_message`` or an API response.

The mechanism is that a failure is *identified* by a :class:`McpErrorCode` and its message is looked
up in a module-private table from that code alone. There is deliberately no constructor that accepts
a message: a caller cannot pass remote text through even by accident, because there is no parameter
for it. The remote detail is not logged either -- it is dropped, because an operator who needs it
can reproduce the failure, and a log line is a leak waiting for a wider sink.

Typed codes still distinguish causes, so an operator can tell a refused origin from an unsupported
protocol without the message having to say so.
"""

from __future__ import annotations

from enum import StrEnum


class McpErrorCode(StrEnum):
    """One typed, durable-safe reason an MCP operation did not produce a usable result.

    These names are persisted verbatim in ``mcp_connections.last_error_code`` and
    ``tool_invocations.error_code``, both bounded at 64 characters, so every member must stay
    comfortably inside that column and must never be composed from remote input.
    """

    # Configuration and egress refusals -- decided entirely by NervOS before any remote effect.
    ORIGIN_REFUSED = "mcp_origin_refused"
    CREDENTIAL_ALIAS_NOT_AVAILABLE = "mcp_credential_alias_not_available"
    SERVER_KEY_UNKNOWN = "mcp_server_key_unknown"

    # Protocol refusals -- the server exists but does not speak the one revision NervOS accepts.
    PROTOCOL_UNSUPPORTED = "mcp_protocol_unsupported"

    # Discovery outcomes.
    CONNECTION_UNAVAILABLE = "mcp_connection_unavailable"
    SERVER_UNAVAILABLE = "mcp_server_unavailable"
    DISCOVERY_TIMEOUT = "mcp_discovery_timeout"
    CATALOG_INVALID = "mcp_catalog_invalid"
    CATALOG_TOO_LARGE = "mcp_catalog_too_large"

    # Execution outcomes.
    TOOL_RESULT_UNSUPPORTED = "mcp_tool_result_unsupported"
    TOOL_FAILED = "mcp_tool_failed"


# The complete static sentence for each code. These strings are the *only* text a durable MCP
# failure may carry, so each is written to be true without knowing anything about the remote side.
_MESSAGES: dict[McpErrorCode, str] = {
    McpErrorCode.ORIGIN_REFUSED: "The MCP endpoint origin is not permitted.",
    McpErrorCode.CREDENTIAL_ALIAS_NOT_AVAILABLE: (
        "The referenced credential alias is not available for this connection."
    ),
    McpErrorCode.SERVER_KEY_UNKNOWN: (
        "The referenced MCP server key is not declared by the operator."
    ),
    McpErrorCode.PROTOCOL_UNSUPPORTED: "The MCP server uses unsupported protocol version.",
    McpErrorCode.CONNECTION_UNAVAILABLE: "The MCP connection is not available.",
    McpErrorCode.SERVER_UNAVAILABLE: "The MCP server could not be reached.",
    McpErrorCode.DISCOVERY_TIMEOUT: "MCP discovery did not complete in time.",
    McpErrorCode.CATALOG_INVALID: "The MCP tool catalog is invalid.",
    McpErrorCode.CATALOG_TOO_LARGE: "Tool catalog exceeds the 128-tool limit.",
    McpErrorCode.TOOL_RESULT_UNSUPPORTED: "Tool returned an unsupported result.",
    McpErrorCode.TOOL_FAILED: "The MCP tool reported a failure.",
}

# Bounded by the durable columns: `error_code` is 1..64 and `error_message` is 1..512 characters.
# Asserted below rather than assumed, because a message that cannot be stored would turn a known
# failure into an unrecorded one at exactly the moment the record matters.
_MAX_CODE_CHARS = 64
_MAX_MESSAGE_CHARS = 512


def _assert_bounds() -> None:
    for code, message in _MESSAGES.items():
        if not 1 <= len(code.value) <= _MAX_CODE_CHARS:  # pragma: no cover - import-time invariant
            raise AssertionError(f"mcp error code out of bounds: {code!r}")
        if not 1 <= len(message) <= _MAX_MESSAGE_CHARS:  # pragma: no cover - import-time invariant
            raise AssertionError(f"mcp error message out of bounds: {code!r}")


_assert_bounds()


class McpError(Exception):
    """A classified, safe MCP failure.

    ``message`` is looked up from :attr:`code`, never supplied by a caller, so no remote text can be
    composed into a persisted or returned error. ``str(error)`` is therefore the static sentence and
    is safe to record verbatim.
    """

    def __init__(self, code: McpErrorCode) -> None:
        self.code = code
        super().__init__(_MESSAGES[code])

    @property
    def message(self) -> str:
        """The fixed, NervOS-authored sentence for this code."""
        return _MESSAGES[self.code]

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.code.value!r})"


class McpConfigurationError(McpError):
    """A refusal decided entirely from NervOS configuration, before any remote effect.

    Raised for a forbidden or unlisted origin, an unknown operator server key, and an unavailable
    credential alias. Nothing has been sent anywhere when this is raised, which is what lets a
    caller classify it as a known pre-dispatch failure.
    """


class McpProtocolError(McpError):
    """The server was reachable but does not speak the one MCP revision NervOS accepts."""


class McpDiscoveryError(McpError):
    """Discovery did not produce a complete, admissible tool catalog."""


class McpToolError(McpError):
    """A ``tools/call`` produced a result NervOS can classify as a failure."""
