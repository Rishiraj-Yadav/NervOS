"""The fake server discovered as a real child process over stdio, through production discovery.

The child is started with ``sys.executable`` and no shell, from the same module the HTTP variant
serves, so a genuine JSON-RPC handshake crosses a pipe. Its ledger is a file the child appends to,
because the test process cannot reach a child's memory. No database and no external network are
involved, and the module skips cleanly if this platform cannot start a subprocess.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from mcp.client import Client
from nervos_core.application.mcp_connections import ConnectionTransport, McpConnectionRow
from nervos_core.domain.tools import DefinitionStatus
from nervos_mcp.discovery import discover_tools
from nervos_mcp.operator_config import McpOperatorConfig, StdioServerSpec
from nervos_mcp.policy.egress import StrictEgressPolicy
from nervos_mcp.transports.stdio import stdio_transport

from ..support.fake_mcp_server import (
    CANONICAL_INPUT_SCHEMAS,
    CANONICAL_OUTPUT_SCHEMA,
    LEDGER_PATH_ENV,
    READ_DOCUMENT,
    WRITE_DOCUMENT,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
SERVER_KEY = "fake-docs"
STDIO_MODULE = "tests.support.fake_stdio_server"


def _subprocess_available() -> bool:
    try:
        completed = subprocess.run(
            [sys.executable, "-c", "pass"],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except OSError:
        return False
    return completed.returncode == 0


pytestmark = pytest.mark.skipif(
    not _subprocess_available(),
    reason="this platform cannot start a Python subprocess",
)


def _stdio_spec(ledger_path: Path) -> StdioServerSpec:
    return StdioServerSpec(
        server_key=SERVER_KEY,
        executable=sys.executable,
        args=("-m", STDIO_MODULE),
        working_dir=str(PACKAGE_ROOT),
        env={"PYTHONPATH": str(PACKAGE_ROOT), LEDGER_PATH_ENV: str(ledger_path)},
    )


@pytest.mark.anyio
async def test_the_fake_catalog_is_discovered_and_normalized_over_stdio(
    tmp_path: Path,
    make_connection: Callable[..., McpConnectionRow],
) -> None:
    operator = McpOperatorConfig(stdio_servers={SERVER_KEY: _stdio_spec(tmp_path / "ledger.txt")})
    connection = make_connection(
        transport=ConnectionTransport.STDIO,
        endpoint=None,
        server_key=SERVER_KEY,
    )

    materials = await discover_tools(
        connection,
        operator=operator,
        policy=StrictEgressPolicy(frozenset()),
    )

    assert [material.upstream_name for material in materials] == [READ_DOCUMENT, WRITE_DOCUMENT]
    assert all(material.status is DefinitionStatus.AVAILABLE for material in materials)
    assert json.loads(materials[0].input_schema) == CANONICAL_INPUT_SCHEMAS[READ_DOCUMENT]
    assert json.loads(materials[1].output_schema or "null") == CANONICAL_OUTPUT_SCHEMA


@pytest.mark.anyio
async def test_the_fake_server_records_writes_over_stdio(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.txt"
    transport = stdio_transport(_stdio_spec(ledger_path))

    async with Client(transport, mode="auto") as client:
        await client.call_tool(WRITE_DOCUMENT, {"path": "a.txt", "content": "one"})
        await client.call_tool(WRITE_DOCUMENT, {"path": "b.txt", "content": "two"})

    assert ledger_path.read_text(encoding="utf-8").splitlines() == [
        "write_document:a.txt=one",
        "write_document:b.txt=two",
    ]
