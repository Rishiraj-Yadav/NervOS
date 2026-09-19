"""Bounded ``tools/list`` discovery, driven by a scripted client so no socket is ever opened.

Two loop guards are proved separately: a cursor handed back twice fails discovery, and a cursor
generator that never repeats is bounded by a page count. The catalog bound is proved at its exact
edges -- 128 with a pending cursor is a refusal, 128 with no cursor is a success, and 129 is a
refusal -- because an off-by-one here decides whether a partial catalog is ever admitted.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.types import Tool, ToolAnnotations
from mcp.types.version import LATEST_MODERN_VERSION
from nervos_core.application.mcp_connections import McpConnectionRow
from nervos_core.application.tool_registry import ToolDefinitionMaterial
from nervos_core.domain.tools import DefinitionStatus, RiskHints
from nervos_mcp.discovery import (
    MAX_DISCOVERY_PAGES,
    MAX_DISCOVERY_TOOLS,
    discover_tools,
    normalize_tool,
)
from nervos_mcp.errors import McpDiscoveryError, McpErrorCode, McpProtocolError
from nervos_mcp.operator_config import McpOperatorConfig
from nervos_mcp.policy.egress import StrictEgressPolicy

CANONICAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

# ``discover_tools`` calls this shape: page index, the cursor it was handed, and what comes back.
Responder = Callable[[int, str | None], tuple[Sequence[Any], str | None]]


def _tool(name: str) -> Tool:
    return Tool(name=name, description="A discovered tool.", input_schema=CANONICAL_SCHEMA)


def _install_client(
    monkeypatch: pytest.MonkeyPatch,
    responder: Responder,
    *,
    protocol_version: str = LATEST_MODERN_VERSION,
) -> list[str | None]:
    """Replace the transport builder and the SDK client, and return the recorded cursors."""
    calls: list[str | None] = []

    class _StubClient:
        def __init__(self, transport: Any, mode: str = "auto") -> None:
            self._transport = transport
            self._mode = mode

        async def __aenter__(self) -> _StubClient:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        @property
        def protocol_version(self) -> str:
            return protocol_version

        async def list_tools(self, *, cursor: str | None = None) -> Any:
            index = len(calls)
            calls.append(cursor)
            tools, next_cursor = responder(index, cursor)
            return SimpleNamespace(tools=list(tools), next_cursor=next_cursor)

    def _build_transport(*args: Any, **kwargs: Any) -> Any:
        return object()

    monkeypatch.setattr("nervos_mcp.discovery.Client", _StubClient)
    monkeypatch.setattr("nervos_mcp.discovery._build_transport", _build_transport)
    return calls


async def _discover(connection: McpConnectionRow) -> tuple[ToolDefinitionMaterial, ...]:
    return await discover_tools(
        connection,
        operator=McpOperatorConfig(),
        policy=StrictEgressPolicy(frozenset()),
    )


@pytest.mark.anyio
async def test_one_page_of_tools_is_returned(
    monkeypatch: pytest.MonkeyPatch,
    make_connection: Callable[..., McpConnectionRow],
) -> None:
    calls = _install_client(monkeypatch, lambda index, cursor: ([_tool("read_document")], None))

    materials = await _discover(make_connection())

    assert [material.upstream_name for material in materials] == ["read_document"]
    assert materials[0].status is DefinitionStatus.AVAILABLE
    assert calls == [None]


@pytest.mark.anyio
async def test_every_page_is_followed_until_the_cursor_ends(
    monkeypatch: pytest.MonkeyPatch,
    make_connection: Callable[..., McpConnectionRow],
) -> None:
    def responder(index: int, cursor: str | None) -> tuple[Sequence[Any], str | None]:
        if index == 0:
            return [_tool("first")], "page-2"
        return [_tool("second")], None

    calls = _install_client(monkeypatch, responder)

    materials = await _discover(make_connection())

    assert [material.upstream_name for material in materials] == ["first", "second"]
    assert calls == [None, "page-2"]


@pytest.mark.anyio
async def test_an_empty_catalog_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
    make_connection: Callable[..., McpConnectionRow],
) -> None:
    calls = _install_client(monkeypatch, lambda index, cursor: ([], None))

    materials = await _discover(make_connection())

    assert materials == ()
    assert calls == [None]


@pytest.mark.anyio
async def test_a_repeated_cursor_fails_discovery(
    monkeypatch: pytest.MonkeyPatch,
    make_connection: Callable[..., McpConnectionRow],
) -> None:
    calls = _install_client(monkeypatch, lambda index, cursor: ([], "same-cursor"))

    with pytest.raises(McpDiscoveryError) as failure:
        await _discover(make_connection())

    assert failure.value.code is McpErrorCode.CATALOG_INVALID
    assert len(calls) == 2


@pytest.mark.anyio
async def test_distinct_cursors_with_empty_pages_are_bounded_by_the_page_count(
    monkeypatch: pytest.MonkeyPatch,
    make_connection: Callable[..., McpConnectionRow],
) -> None:
    calls = _install_client(monkeypatch, lambda index, cursor: ([], f"cursor-{index}"))

    with pytest.raises(McpDiscoveryError) as failure:
        await _discover(make_connection())

    assert failure.value.code is McpErrorCode.CATALOG_INVALID
    assert len(calls) == MAX_DISCOVERY_PAGES


@pytest.mark.anyio
async def test_exactly_the_tool_limit_with_no_cursor_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    make_connection: Callable[..., McpConnectionRow],
) -> None:
    tools = [_tool(f"tool_{index}") for index in range(MAX_DISCOVERY_TOOLS)]
    calls = _install_client(monkeypatch, lambda index, cursor: (tools, None))

    materials = await _discover(make_connection())

    assert len(materials) == MAX_DISCOVERY_TOOLS
    assert calls == [None]


@pytest.mark.anyio
async def test_exactly_the_tool_limit_with_a_pending_cursor_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    make_connection: Callable[..., McpConnectionRow],
) -> None:
    tools = [_tool(f"tool_{index}") for index in range(MAX_DISCOVERY_TOOLS)]
    calls = _install_client(monkeypatch, lambda index, cursor: (tools, "more"))

    with pytest.raises(McpDiscoveryError) as failure:
        await _discover(make_connection())

    assert failure.value.code is McpErrorCode.CATALOG_TOO_LARGE
    assert calls == [None]


@pytest.mark.anyio
async def test_more_than_the_tool_limit_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    make_connection: Callable[..., McpConnectionRow],
) -> None:
    tools = [_tool(f"tool_{index}") for index in range(MAX_DISCOVERY_TOOLS + 1)]
    _install_client(monkeypatch, lambda index, cursor: (tools, None))

    with pytest.raises(McpDiscoveryError) as failure:
        await _discover(make_connection())

    assert failure.value.code is McpErrorCode.CATALOG_TOO_LARGE


@pytest.mark.anyio
async def test_duplicate_upstream_names_are_refused(
    monkeypatch: pytest.MonkeyPatch,
    make_connection: Callable[..., McpConnectionRow],
) -> None:
    _install_client(monkeypatch, lambda index, cursor: ([_tool("dup"), _tool("dup")], None))

    with pytest.raises(McpDiscoveryError) as failure:
        await _discover(make_connection())

    assert failure.value.code is McpErrorCode.CATALOG_INVALID


@pytest.mark.anyio
async def test_a_non_modern_protocol_version_refuses_before_any_listing(
    monkeypatch: pytest.MonkeyPatch,
    make_connection: Callable[..., McpConnectionRow],
) -> None:
    calls = _install_client(
        monkeypatch,
        lambda index, cursor: ([_tool("never_listed")], None),
        protocol_version="2025-06-18",
    )

    with pytest.raises(McpProtocolError) as failure:
        await _discover(make_connection())

    assert failure.value.code is McpErrorCode.PROTOCOL_UNSUPPORTED
    assert calls == []


def test_normalize_tool_marks_an_unsupported_input_schema() -> None:
    tool = Tool(
        name="bad-input",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "title": "not-in-the-canonical-subset",
        },
    )

    material = normalize_tool(tool, 1, "bad-input")

    assert material.status is DefinitionStatus.UNSUPPORTED_SCHEMA


def test_normalize_tool_marks_an_unsupported_advertised_output_schema() -> None:
    tool = Tool(
        name="bad-output",
        input_schema=CANONICAL_SCHEMA,
        output_schema={"type": "object", "properties": {}, "title": "not-canonical"},
    )

    material = normalize_tool(tool, 1, "bad-output")

    assert material.status is DefinitionStatus.UNSUPPORTED_SCHEMA


def test_normalize_tool_admits_a_canonical_definition() -> None:
    tool = Tool(name="read_document", description="Returns text.", input_schema=CANONICAL_SCHEMA)

    material = normalize_tool(tool, 7, "read_document")

    assert material.status is DefinitionStatus.AVAILABLE
    assert material.source_id == 7
    assert material.upstream_name == "read_document"
    assert material.output_schema is None


def test_normalize_tool_maps_every_annotation_onto_a_risk_hint() -> None:
    tool = Tool(
        name="annotated",
        input_schema=CANONICAL_SCHEMA,
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )

    material = normalize_tool(tool, 1, "annotated")

    assert material.risk_hints == RiskHints(
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
    )
