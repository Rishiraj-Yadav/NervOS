"""The fake MCP server the integration tests discover through, built on the official SDK.

It is intentionally small and deterministic: two tools, ``read_document`` and ``write_document``,
where writing appends to a ledger the test process can observe. It is served two ways from the same
module -- as a real subprocess over stdio (:mod:`tests.support.fake_stdio_server`) and over
Streamable HTTP on a dynamically chosen loopback port (:func:`serve_streamable_http`).

**Why the schemas are rewritten.** The SDK derives a JSON schema from the Python signature, and that
schema carries ``title`` keywords and omits ``additionalProperties: false``. NervOS's canonical
subset refuses both, so a plain SDK server would be discovered as ``unsupported_schema``. The
override below advertises the same callable contract in the canonical form a real, D3-compatible
server would publish, which is what lets the integration test prove an *admissible* catalog rather
than a refusal. Argument validation still runs against the SDK's own model, so the tools really
execute.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.types import Tool, ToolAnnotations

from .egress import LOOPBACK_HOST

READ_DOCUMENT = "read_document"
WRITE_DOCUMENT = "write_document"
TOOL_NAMES = (READ_DOCUMENT, WRITE_DOCUMENT)

# The stdio variant runs in a child process, so its ledger is written to a file this variable names.
LEDGER_PATH_ENV = "NERVOS_FAKE_MCP_LEDGER_PATH"

CANONICAL_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"result": {"type": "string"}},
    "required": ["result"],
    "additionalProperties": False,
}

CANONICAL_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    READ_DOCUMENT: {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    },
    WRITE_DOCUMENT: {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
        "additionalProperties": False,
    },
}


@dataclass
class Ledger:
    """The observable record of tool calls: counts and entries, in memory and optionally in a file.

    The file sink exists for the stdio variant, whose server lives in another process and whose
    in-memory state the test cannot reach.
    """

    path: Path | None = None
    entries: list[str] = field(default_factory=list[str])
    counts: dict[str, int] = field(default_factory=dict[str, int])

    def record(self, tool: str, entry: str) -> None:
        self.counts[tool] = self.counts.get(tool, 0) + 1
        self.entries.append(f"{tool}:{entry}")
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(f"{tool}:{entry}\n")


async def _hold(gate: asyncio.Event | None) -> None:
    """Hold a tool call open until the test releases it, when a gate was supplied.

    The failure suites need a *deterministic* long call -- one whose completion the test decides,
    not one that a wall-clock sleep happens to end. A tool that awaits this gate has provably
    reached the server (its ledger entry is written first) while its reply is still in flight, so a
    test can cancel, expire, or time out the caller without racing anything. With no gate the call
    returns immediately, which is every other test's behaviour.
    """
    if gate is not None:
        await gate.wait()


class FakeMcpServer(MCPServer[Any]):
    """The two-tool fake server, advertising canonical schemas over the ordinary SDK surface."""

    def __init__(self, ledger: Ledger | None = None, *, gate: asyncio.Event | None = None) -> None:
        super().__init__(
            name="nervos-fake-mcp",
            version="0.1.0",
            description="Deterministic document tools for the NervOS MCP integration tests.",
        )
        self.ledger = ledger if ledger is not None else Ledger()
        self.gate = gate
        self._register_tools()

    async def list_tools(self) -> list[Tool]:
        """Advertise each tool's callable contract in the canonical schema subset."""
        tools = await super().list_tools()
        return [
            tool.model_copy(
                update={
                    "input_schema": CANONICAL_INPUT_SCHEMAS[tool.name],
                    "output_schema": CANONICAL_OUTPUT_SCHEMA,
                }
            )
            if tool.name in CANONICAL_INPUT_SCHEMAS
            else tool
            for tool in tools
        ]

    def _register_tools(self) -> None:
        ledger = self.ledger
        gate = self.gate

        @self.tool(
            name=READ_DOCUMENT,
            title="Read Document",
            description="Return deterministic text for a document path.",
            annotations=ToolAnnotations(
                read_only_hint=True,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=False,
            ),
        )
        async def read_document(path: str) -> str:  # pyright: ignore[reportUnusedFunction]
            """Return deterministic text for a document path."""
            ledger.record(READ_DOCUMENT, path)
            await _hold(gate)
            return f"document:{path}"

        @self.tool(
            name=WRITE_DOCUMENT,
            title="Write Document",
            description="Append content to the document ledger and confirm the write.",
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=True,
                idempotent_hint=False,
                open_world_hint=False,
            ),
        )
        async def write_document(path: str, content: str) -> str:  # pyright: ignore[reportUnusedFunction]
            """Append content to the document ledger and confirm the write."""
            ledger.record(WRITE_DOCUMENT, f"{path}={content}")
            await _hold(gate)
            return f"wrote:{path}"


def build_server(
    *, ledger_path: str | None = None, gate: asyncio.Event | None = None
) -> FakeMcpServer:
    """Build the fake server, taking the file ledger path from the argument or the environment.

    ``gate`` is the opt-in release seam the failure suites hold a call open with; it defaults to
    ``None`` so every existing caller keeps an immediately-returning server.
    """
    if ledger_path is None:
        raw = os.environ.get(LEDGER_PATH_ENV)
        ledger_path = raw if raw else None
    return FakeMcpServer(
        Ledger(path=Path(ledger_path) if ledger_path is not None else None), gate=gate
    )


def _free_loopback_port() -> int:
    """Ask the OS for one currently-unused loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK_HOST, 0))
        return int(probe.getsockname()[1])


async def _await_started(server: uvicorn.Server, task: asyncio.Task[None]) -> None:
    """Wait a bounded moment for uvicorn to report itself started."""
    for _ in range(500):
        if server.started:
            return
        if task.done():
            raise RuntimeError("the fake MCP HTTP server exited before it started")
        await asyncio.sleep(0.02)
    raise RuntimeError("the fake MCP HTTP server did not start in time")


@contextlib.asynccontextmanager
async def serve_streamable_http(server: FakeMcpServer) -> AsyncGenerator[str, None]:
    """Serve ``server`` over Streamable HTTP and yield its endpoint URL.

    The port is chosen at runtime, so two test runs -- or two tests in one run -- never collide.
    """
    port = _free_loopback_port()
    app = server.streamable_http_app(streamable_http_path="/mcp", host=LOOPBACK_HOST)
    config = uvicorn.Config(app, host=LOOPBACK_HOST, port=port, log_level="warning")
    http_server = uvicorn.Server(config)
    task = asyncio.create_task(http_server.serve())
    await _await_started(http_server, task)
    try:
        yield f"http://{LOOPBACK_HOST}:{port}/mcp"
    finally:
        http_server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=10)


__all__ = [
    "CANONICAL_INPUT_SCHEMAS",
    "CANONICAL_OUTPUT_SCHEMA",
    "LEDGER_PATH_ENV",
    "READ_DOCUMENT",
    "TOOL_NAMES",
    "WRITE_DOCUMENT",
    "FakeMcpServer",
    "Ledger",
    "build_server",
    "serve_streamable_http",
]
