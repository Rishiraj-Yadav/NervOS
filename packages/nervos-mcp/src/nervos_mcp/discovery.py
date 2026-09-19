"""Bounded ``tools/list`` discovery, normalized into durable tool material.

Discovery is the one operation that reads a remote catalog, and it is written so that a hostile or
broken server cannot make it run long, run large, or run forever:

**One deadline for the whole operation.** Connect, negotiate and every page share a single
:data:`MCP_DISCOVERY_TIMEOUT_SECONDS` budget, so pagination cannot extend discovery past its bound
by returning one more page each time.

**Two independent loop guards.** A repeated non-null cursor fails discovery outright -- a server
that hands back a cursor it already gave is looping, and following it is how a client spins -- and a
page count is capped as well, because a server can also emit infinitely many *distinct* cursors with
nothing behind them.

**A catalog that would exceed the bound fails.** Reaching exactly the limit with a cursor still
pending is not success: the cursor proves the remote catalog holds more, so the connection is
refused rather than truncated. Nothing partial is ever admitted, which is what keeps a run's tool
set from depending on the order a server happened to enumerate in.

**Nothing is persisted here.** The result is durable *material*; writing it, and marking the
connection, is one transaction in core. Nothing discovered becomes authoritative until that commit.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any, cast

from mcp import Client
from mcp.types.version import LATEST_MODERN_VERSION
from nervos_core.application.mcp_connections import (
    ConnectionTransport,
    McpConnectionRow,
)
from nervos_core.application.tool_registry import ToolDefinitionMaterial
from nervos_core.application.tool_schema import SchemaRejection, validate_canonical_schema
from nervos_core.domain.tools import (
    DefinitionStatus,
    JsonValue,
    RiskHints,
    ToolSourceKind,
    ToolSourceRef,
    canonical_json_text,
    definition_fingerprint,
    model_tool_name,
    require_json_value,
)

from nervos_mcp.errors import (
    McpDiscoveryError,
    McpError,
    McpErrorCode,
    McpProtocolError,
)
from nervos_mcp.operator_config import McpOperatorConfig
from nervos_mcp.policy.egress import EgressPolicy
from nervos_mcp.transports.http import http_transport
from nervos_mcp.transports.stdio import stdio_transport

# The whole-operation deadline. Connect, negotiation and every page share it.
MCP_DISCOVERY_TIMEOUT_SECONDS = 30.0
# The admission bound, and the page bound that keeps a cursor generator finite.
MAX_DISCOVERY_TOOLS = 128
MAX_DISCOVERY_PAGES = 128
# The stored schema for a definition NervOS cannot admit. It is a *valid* canonical schema (the
# closed empty object), so D3's fingerprint helper has something real to hash, but the definition's
# status is `unsupported_schema`, so it is never offered to a model and D2 denies any grant on it.
_UNSUPPORTED_SCHEMA_SENTINEL = '{"additionalProperties":false,"properties":{},"type":"object"}'
# The display name bound the durable column enforces.
_MAX_DISPLAY_NAME_CHARS = 100


# A discovered catalog is exactly a sequence of definition material -- the same pre-persistence
# shape reconciliation already writes. There is no separate "discovered" type, because a second
# shape for the same fields would only create a mapping step where the two could drift apart.
DiscoveredDefinition = ToolDefinitionMaterial


def _display_name(tool: Any, upstream_name: str) -> str:
    """The operator-visible label: a bounded server-supplied title, else the upstream name.

    A title is remote input, so it is bounded and stripped before use and falls back rather than
    failing: a server choosing an unusable label must not be able to make a tool undiscoverable.
    """
    title = getattr(tool, "title", None)
    if isinstance(title, str):
        candidate = title.strip()
        if 1 <= len(candidate) <= _MAX_DISPLAY_NAME_CHARS:
            return candidate
    return upstream_name[:_MAX_DISPLAY_NAME_CHARS]


def _risk_hints(tool: Any) -> RiskHints:
    """Read the server's annotation claims, defaulting to the alarming reading.

    The defaults match the MCP specification's own conservative reading and D1's column defaults: an
    unannotated tool is stored as possibly destructive and possibly open-world. These are claims for
    a human to read, never authority -- nothing downstream consults them to permit an action.
    """
    annotations = getattr(tool, "annotations", None)
    if annotations is None:
        return RiskHints()
    return RiskHints(
        read_only=bool(getattr(annotations, "read_only_hint", False)),
        destructive=bool(getattr(annotations, "destructive_hint", True)),
        idempotent=bool(getattr(annotations, "idempotent_hint", False)),
        open_world=bool(getattr(annotations, "open_world_hint", True)),
    )


def _canonical_schema_text(schema: Mapping[str, Any] | None) -> str | None:
    """Return canonical JSON text for a schema, or ``None`` when it is not admissible.

    The schema is normalized through the JSON value contract first, so a non-JSON construct fails
    here rather than at a later persistence boundary, and the text is canonical so the same schema
    always hashes identically.
    """
    if schema is None:
        return None
    try:
        value = cast("JsonValue", dict(schema))
        require_json_value(value, path="$.schema")
    except Exception:
        return None
    if isinstance(validate_canonical_schema(value), SchemaRejection):
        return None
    return canonical_json_text(value)


def normalize_tool(tool: Any, connection_id: int, upstream_name: str) -> ToolDefinitionMaterial:
    """Turn one SDK ``Tool`` into durable material, or mark the whole definition unsupported.

    A definition is ``unsupported_schema`` when *any* schema NervOS is required to rely on cannot be
    admitted -- the input schema, or an advertised output schema. The advertised output schema is
    not dropped to make the tool available: a model that cannot be told what a tool returns cannot
    be given it safely, and quietly discarding the schema would offer a tool whose contract NervOS
    has already failed to understand. The raw remote schema is never persisted.
    """
    input_schema_text = _canonical_schema_text(cast("Mapping[str, Any]", tool.input_schema))
    raw_output = getattr(tool, "output_schema", None)
    output_schema_text = (
        None
        if raw_output is None
        else _canonical_schema_text(cast("Mapping[str, Any]", raw_output))
    )
    admissible = input_schema_text is not None and (
        raw_output is None or output_schema_text is not None
    )
    status = DefinitionStatus.AVAILABLE if admissible else DefinitionStatus.UNSUPPORTED_SCHEMA
    # `admissible` is exactly `input_schema_text is not None and ...`, so the cast records a fact
    # this function has already established rather than asserting one.
    stored_input = (
        _UNSUPPORTED_SCHEMA_SENTINEL if not admissible else cast("str", input_schema_text)
    )
    stored_output = output_schema_text if admissible else None
    description = tool.description if isinstance(tool.description, str) else ""
    display = _display_name(tool, upstream_name)
    name = model_tool_name(ToolSourceRef(ToolSourceKind.MCP, connection_id), upstream_name)
    hints = _risk_hints(tool)
    fingerprint = definition_fingerprint(
        model_name=name,
        upstream_name=upstream_name,
        description=description,
        input_schema=cast("dict[str, JsonValue]", json.loads(stored_input)),
        output_schema=(
            None
            if stored_output is None
            else cast("dict[str, JsonValue]", json.loads(stored_output))
        ),
        source_kind=ToolSourceKind.MCP,
        source_id=connection_id,
        risk_hints=hints,
    )
    return ToolDefinitionMaterial(
        source_kind=ToolSourceKind.MCP,
        source_id=connection_id,
        upstream_name=upstream_name,
        model_name=name,
        display_name=display,
        description=description,
        input_schema=stored_input,
        output_schema=stored_output,
        status=status,
        risk_hints=hints,
        fingerprint=fingerprint,
    )


async def _collect_tools(client: Client) -> tuple[Any, ...]:
    """Read every page of ``tools/list`` under both loop guards, or fail discovery."""
    collected: list[Any] = []
    seen_cursors: set[str] = set()
    cursor: str | None = None
    pages = 0
    while True:
        result = await client.list_tools(cursor=cursor)
        pages += 1
        page_tools = list(result.tools)
        if len(collected) + len(page_tools) > MAX_DISCOVERY_TOOLS:
            # This covers the boundary case too: a full page that lands exactly on the limit while a
            # cursor is still pending is refused by the check below, not accepted here.
            raise McpDiscoveryError(McpErrorCode.CATALOG_TOO_LARGE)
        collected.extend(page_tools)
        next_cursor = getattr(result, "next_cursor", None)
        if next_cursor is None:
            return tuple(collected)
        if len(collected) >= MAX_DISCOVERY_TOOLS:
            # The remote catalog holds more than the bound, whichever page it would arrive on.
            raise McpDiscoveryError(McpErrorCode.CATALOG_TOO_LARGE)
        if next_cursor in seen_cursors:
            raise McpDiscoveryError(McpErrorCode.CATALOG_INVALID)
        if pages >= MAX_DISCOVERY_PAGES:
            raise McpDiscoveryError(McpErrorCode.CATALOG_INVALID)
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def _build_transport(
    connection: McpConnectionRow,
    operator: McpOperatorConfig,
    policy: EgressPolicy,
) -> Any:
    """Build the transport for one connection, resolving the credential at the last moment.

    Everything that can be refused from configuration alone -- an undeclared server key, an alias
    that is missing or not bound to this target, a forbidden origin -- is refused here, before a
    process is started or a socket exists.
    """
    if connection.transport is ConnectionTransport.STDIO:
        spec = operator.stdio_server(connection.server_key or "")
        credential = None
        if connection.credential_ref is not None:
            alias = operator.credential_alias(connection.credential_ref)
            secret = operator.resolve_secret(connection.credential_ref, spec.server_key)
            credential = (alias.env_var, secret)
        return stdio_transport(spec, credential=credential)
    endpoint = connection.endpoint or ""
    secret_value = None
    if connection.credential_ref is not None:
        # Bound to the connection's target, re-proved now rather than trusted from the row.
        secret_value = operator.resolve_secret(connection.credential_ref, endpoint)
    return http_transport(endpoint, policy, credential=secret_value)


async def discover_tools(
    connection: McpConnectionRow,
    *,
    operator: McpOperatorConfig,
    policy: EgressPolicy,
) -> tuple[ToolDefinitionMaterial, ...]:
    """Discover one connection's tools, normalized, under a single bounded deadline.

    The negotiated protocol revision is checked immediately after the connection is established and
    before any catalog request: a server that negotiated anything other than the one modern revision
    NervOS implements is refused, and no ``tools/list`` is attempted against it. The refused version
    is never interpolated into a message.
    """
    try:
        async with asyncio.timeout(MCP_DISCOVERY_TIMEOUT_SECONDS):
            transport = _build_transport(connection, operator, policy)
            async with Client(transport, mode="auto") as client:
                if client.protocol_version != LATEST_MODERN_VERSION:
                    raise McpProtocolError(McpErrorCode.PROTOCOL_UNSUPPORTED)
                remote_tools = await _collect_tools(client)
    except McpError:
        raise
    except TimeoutError as error:
        raise McpDiscoveryError(McpErrorCode.DISCOVERY_TIMEOUT) from error
    except asyncio.CancelledError:
        raise
    except Exception as error:
        # Any other failure -- a refused connection, a transport fault, a malformed frame -- is an
        # unreachable server as far as this process can tell. The original exception is not
        # propagated, because its text is remote-controlled.
        raise McpDiscoveryError(McpErrorCode.SERVER_UNAVAILABLE) from error

    upstream_names = [tool.name for tool in remote_tools]
    if len(set(upstream_names)) != len(upstream_names):
        # One connection's catalog must be a function of its own names; a duplicate makes which
        # definition a name refers to depend on page order.
        raise McpDiscoveryError(McpErrorCode.CATALOG_INVALID)
    return tuple(normalize_tool(tool, connection.connection_id, tool.name) for tool in remote_tools)


__all__ = [
    "MAX_DISCOVERY_PAGES",
    "MAX_DISCOVERY_TOOLS",
    "MCP_DISCOVERY_TIMEOUT_SECONDS",
    "DiscoveredDefinition",
    "discover_tools",
    "normalize_tool",
]
