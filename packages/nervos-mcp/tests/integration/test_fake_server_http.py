"""The fake server discovered as a real HTTP peer on loopback, through production discovery.

Nothing here mocks the wire: uvicorn serves the official SDK app on a port chosen at runtime, and
``discover_tools`` reaches it through the production HTTP transport. The only substitution is the
egress policy, which is the test-only loopback policy because production refuses plain ``http://``
outright. No database and no external network are involved.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from urllib.parse import urlsplit

import pytest
from mcp.client import Client
from nervos_core.application.mcp_connections import McpConnectionRow
from nervos_core.domain.tools import DefinitionStatus, RiskHints
from nervos_mcp.discovery import discover_tools
from nervos_mcp.operator_config import McpOperatorConfig
from nervos_mcp.transports.http import http_transport

from ..support.egress import LoopbackEgressPolicy
from ..support.fake_mcp_server import (
    CANONICAL_INPUT_SCHEMAS,
    CANONICAL_OUTPUT_SCHEMA,
    READ_DOCUMENT,
    WRITE_DOCUMENT,
    build_server,
    serve_streamable_http,
)


def _port(endpoint: str) -> int:
    port = urlsplit(endpoint).port
    assert port is not None
    return port


@pytest.mark.anyio
async def test_the_fake_catalog_is_discovered_and_normalized_over_http(
    make_connection: Callable[..., McpConnectionRow],
) -> None:
    server = build_server()
    async with serve_streamable_http(server) as endpoint:
        connection = make_connection(endpoint=endpoint)
        materials = await discover_tools(
            connection,
            operator=McpOperatorConfig(),
            policy=LoopbackEgressPolicy({_port(endpoint)}),
        )

    assert [material.upstream_name for material in materials] == [READ_DOCUMENT, WRITE_DOCUMENT]
    assert all(material.status is DefinitionStatus.AVAILABLE for material in materials)
    assert all(material.source_id == connection.connection_id for material in materials)

    read, write = materials
    assert json.loads(read.input_schema) == CANONICAL_INPUT_SCHEMAS[READ_DOCUMENT]
    assert json.loads(write.input_schema) == CANONICAL_INPUT_SCHEMAS[WRITE_DOCUMENT]
    assert json.loads(read.output_schema or "null") == CANONICAL_OUTPUT_SCHEMA
    assert read.display_name == "Read Document"
    assert read.risk_hints == RiskHints(
        read_only=True, destructive=False, idempotent=True, open_world=False
    )
    assert write.risk_hints == RiskHints(
        read_only=False, destructive=True, idempotent=False, open_world=False
    )


@pytest.mark.anyio
async def test_the_fake_server_records_writes_reached_through_the_http_transport() -> None:
    server = build_server()
    async with serve_streamable_http(server) as endpoint:
        transport = http_transport(endpoint, LoopbackEgressPolicy({_port(endpoint)}))
        async with Client(transport, mode="auto") as client:
            first = await client.call_tool(WRITE_DOCUMENT, {"path": "a.txt", "content": "one"})
            second = await client.call_tool(WRITE_DOCUMENT, {"path": "b.txt", "content": "two"})

    assert first.is_error is False
    assert second.is_error is False
    assert server.ledger.counts[WRITE_DOCUMENT] == 2
    assert server.ledger.entries == [
        "write_document:a.txt=one",
        "write_document:b.txt=two",
    ]
