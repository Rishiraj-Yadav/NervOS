"""``McpGateway``: live MCP clients, owned by one process, authoritative over nothing.

The gateway is the only place that holds a connected MCP client across calls. Three rules keep that
from becoming a correctness dependency:

**A cached client is an optimization, never an authority.** Whether a call may happen is decided by
D2's live evaluator reading durable state, immediately before dispatch. A cached session that is
stale, disabled, or belongs to a connection another Worker just deleted therefore cannot authorize
anything -- and that is why no cache invalidation *message* is needed between Workers for safety.
Only local cleanup is.

**Idle sessions are closed lazily, with a single bound.** A cached client older than
:data:`IDLE_EXPIRY_SECONDS` is closed and replaced on next use, which bounds how long a server sees
one client without introducing a background reaper, a timer, or a task that could fail on its own.

**Ambiguity is preserved across the boundary.** Anything raised after ``call_tool`` is entered does
not prove the request was never sent, so it becomes :class:`ToolOutcomeUnknown` rather than a clean
failure. Failures *before* that point -- a missing row, a disabled connection, an unavailable alias,
a refused origin, a refused protocol revision -- are known, and stay known.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp import Client
from mcp.types.version import LATEST_MODERN_VERSION
from nervos_core.application.mcp_connections import (
    ConnectionStatus,
    McpConnectionPersistence,
    McpConnectionRow,
)
from nervos_core.application.tool_registry import ToolOutcomeUnknown

from nervos_mcp.errors import (
    McpConfigurationError,
    McpErrorCode,
    McpProtocolError,
)
from nervos_mcp.operator_config import McpOperatorConfig
from nervos_mcp.policy.egress import EgressPolicy
from nervos_mcp.transports.http import http_transport
from nervos_mcp.transports.stdio import stdio_transport

# How long a cached client may sit unused before the next call replaces it. Ten minutes is long
# enough that a burst of tool calls reuses one session and short enough that a server is not holding
# a connection for a Worker that has moved on.
IDLE_EXPIRY_SECONDS = 600.0


@dataclass(slots=True)
class _LiveSession:
    """One connected client, plus the stack that owns its transport and its lock."""

    client: Client
    stack: AsyncExitStack
    lock: asyncio.Lock
    last_used: float


class McpGateway:
    """Live MCP clients for one process, keyed by durable connection id."""

    def __init__(
        self,
        *,
        connections: McpConnectionPersistence,
        operator: McpOperatorConfig,
        policy: EgressPolicy,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._connections = connections
        self._operator = operator
        self._policy = policy
        self._clock = clock
        self._sessions: dict[int, _LiveSession] = {}

    async def call_tool(
        self, connection_id: int, upstream_name: str, arguments: dict[str, Any]
    ) -> Any:
        """Call one tool on one connection, or classify why the call could not be made."""
        connection = self._require_connection(connection_id)
        session = await self._session_for(connection)
        try:
            return await session.client.call_tool(upstream_name, arguments)
        except asyncio.CancelledError:
            # The caller withdrew: the invocation stays truthfully `started` and the durable owner
            # settles it. Swallowing this would turn a shutdown into a tool failure.
            raise
        except Exception as error:
            # Entering `call_tool` is the ambiguity boundary. An exception here does not prove the
            # request was never sent, and its text is not evidence either way, so the session is
            # discarded and the outcome reported as unknown rather than guessed at.
            await self.invalidate(connection_id)
            raise ToolOutcomeUnknown() from error

    def _require_connection(self, connection_id: int) -> McpConnectionRow:
        """Read the durable row, refusing anything that must not be executed right now.

        Every refusal here happens before a transport exists, which is what makes it a known
        pre-dispatch failure. The enabled/catalog checks are a courtesy to that classification, not
        the authority: D2 has already decided whether the call may run.
        """
        connection = self._connections.get_by_id(connection_id)
        if connection is None:
            raise McpConfigurationError(McpErrorCode.CONNECTION_UNAVAILABLE)
        if not connection.enabled or connection.catalog_status is ConnectionStatus.DISABLED:
            raise McpConfigurationError(McpErrorCode.CONNECTION_UNAVAILABLE)
        return connection

    async def _session_for(self, connection: McpConnectionRow) -> _LiveSession:
        """Return a usable session, replacing one that has gone idle."""
        existing = self._sessions.get(connection.connection_id)
        if existing is not None and self._clock() - existing.last_used >= IDLE_EXPIRY_SECONDS:
            await self.invalidate(connection.connection_id)
            existing = None
        if existing is not None:
            existing.last_used = self._clock()
            return existing
        # One creation per connection: concurrent callers wait on the same lock rather than racing
        # to start two processes or two HTTP sessions. The lock is per *creation*, not per call, so
        # it is not retained in the cache.
        return await self._create_session(connection, asyncio.Lock())

    async def _create_session(
        self, connection: McpConnectionRow, lock: asyncio.Lock
    ) -> _LiveSession:
        async with lock:
            cached = self._sessions.get(connection.connection_id)
            if cached is not None:
                cached.last_used = self._clock()
                return cached
            stack = AsyncExitStack()
            try:
                client = await self._open_client(stack, connection)
            except BaseException:
                await stack.aclose()
                raise
            session = _LiveSession(client=client, stack=stack, lock=lock, last_used=self._clock())
            self._sessions[connection.connection_id] = session
            return session

    async def _open_client(self, stack: AsyncExitStack, connection: McpConnectionRow) -> Client:
        """Build and enter one client, enforcing the one accepted protocol revision."""
        if connection.transport.value == "stdio":
            spec = self._operator.stdio_server(connection.server_key or "")
            credential = None
            if connection.credential_ref is not None:
                alias = self._operator.credential_alias(connection.credential_ref)
                secret = self._operator.resolve_secret(connection.credential_ref, spec.server_key)
                credential = (alias.env_var, secret)
            transport: Any = stdio_transport(spec, credential=credential)
        else:
            endpoint = connection.endpoint or ""
            secret_value = None
            if connection.credential_ref is not None:
                secret_value = self._operator.resolve_secret(connection.credential_ref, endpoint)
            transport = http_transport(endpoint, self._policy, credential=secret_value)
        client = await stack.enter_async_context(Client(transport, mode="auto"))
        if client.protocol_version != LATEST_MODERN_VERSION:
            # Negotiated something else: close it and refuse, without attempting any operation.
            raise McpProtocolError(McpErrorCode.PROTOCOL_UNSUPPORTED)
        return client

    async def invalidate(self, connection_id: int) -> None:
        """Close and forget the cached session for one connection, if it has one.

        Sessions are closed **newest first**, and invalidating one therefore also drops every
        session created after it. That is a correctness requirement rather than tidiness: each
        client entered its transport inside an anyio cancel scope in this task, cancel scopes must
        unwind in the reverse of the order they were entered, and attempting to exit a scope that is
        not the innermost raises ``RuntimeError``. Dropping the newer sessions is safe -- the cache
        is an optimization and never authority, so the worst case is one reconnect -- and they are
        the sessions that must be released before the target can be reached at all.
        """
        if connection_id not in self._sessions:
            return
        while self._sessions:
            newest = next(reversed(self._sessions))
            session = self._sessions.pop(newest)
            await session.stack.aclose()
            if newest == connection_id:
                return

    async def close_all(self) -> None:
        """Close every cached session, newest first. Called at Worker shutdown."""
        while self._sessions:
            await self.invalidate(next(reversed(self._sessions)))


__all__ = ["IDLE_EXPIRY_SECONDS", "McpGateway"]
