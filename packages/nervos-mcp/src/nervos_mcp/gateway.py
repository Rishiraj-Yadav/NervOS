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
    """One connected client, and the task that owns the context it lives in.

    ``owner`` is not bookkeeping: an anyio cancel scope belongs to the task that entered it, so the
    task that built this client is the only one that can close it. Every close therefore signals
    ``closable`` and awaits ``owner`` rather than touching the transport from the caller's task --
    which is what lets the Worker's shutdown task close a session an Attempt created.
    """

    client: Client
    owner: asyncio.Task[None]
    closable: asyncio.Event
    last_used: float


@dataclass(slots=True)
class _PendingCreation:
    """A creation in flight: the outcome every waiting caller awaits, and the task producing it."""

    ready: asyncio.Future[_LiveSession]
    owner: asyncio.Task[None]


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
        # One *stable* creation lock per connection. It must outlive any single session, because
        # the thing it protects is the act of creating one: a lock held on the session object
        # cannot serialise the callers who arrive before that session exists.
        self._creation_locks: dict[int, asyncio.Lock] = {}
        # The in-flight creation for a connection, if any. It exists so the *outcome* of a
        # creation is shared rather than merely its entry: a caller that is cancelled while
        # waiting releases the lock on its way out, and the next caller must join the creation
        # already running instead of starting a second one. It also holds the owner task from the
        # moment it is created, so a creation in progress can never be collected.
        self._pending: dict[int, _PendingCreation] = {}

    def _creation_lock(self, connection_id: int) -> asyncio.Lock:
        """Return this connection's one creation lock, creating it on first use.

        Deliberately not `async`: there is no `await` between the lookup and the insert, so two
        concurrent callers cannot each install their own lock. They either see the same existing
        lock or race through a sequence in which no other coroutine can run.

        The lock is never removed -- not by `invalidate`, not by `close_all`. Dropping it while a
        caller still waits on the old object would let a later caller install a second lock and
        create a second session, which is the defect this map exists to prevent. The object is a
        few dozen bytes per connection that has ever been used, which is the right trade against
        correctness.
        """
        lock = self._creation_locks.get(connection_id)
        if lock is None:
            lock = asyncio.Lock()
            self._creation_locks[connection_id] = lock
        return lock

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
        """Return a usable session, replacing one that has gone idle or whose owner has died."""
        existing = self._sessions.get(connection.connection_id)
        if existing is not None and (
            not self._usable(existing) or self._clock() - existing.last_used >= IDLE_EXPIRY_SECONDS
        ):
            await self.invalidate(connection.connection_id)
            existing = None
        if existing is not None:
            existing.last_used = self._clock()
            return existing
        return await self._create_session(connection)

    def _usable(self, session: _LiveSession) -> bool:
        """Whether a cached session may be handed to a caller.

        An owner that has finished means its client context is gone. That is not something a caller
        can use, and it is not something to await a second time, so it is treated as absent and the
        next call rebuilds normally rather than handing back a dead client.
        """
        return not session.owner.done()

    async def _create_session(self, connection: McpConnectionRow) -> _LiveSession:
        """Create this connection's session, exactly one per connection, whatever the race.

        The creation lock serialises the *decision*, and the pending future makes the *outcome*
        shared. Both are needed: a caller cancelled while waiting releases the lock as it unwinds,
        so without the future the next caller would see an empty cache and start a second creation
        for a connection that is already being connected.

        The await is deliberately outside the lock -- the owner task, not this caller, is what
        performs the connection, and a caller that goes away must not hold up the Worker-owned
        creation every other caller is waiting on.
        """
        connection_id = connection.connection_id
        async with self._creation_lock(connection_id):
            cached = self._sessions.get(connection_id)
            if cached is not None and self._usable(cached):
                cached.last_used = self._clock()
                return cached
            pending = self._pending.get(connection_id)
            if pending is None:
                ready: asyncio.Future[_LiveSession] = asyncio.get_running_loop().create_future()
                pending = _PendingCreation(
                    ready=ready,
                    owner=asyncio.create_task(self._own_session(connection, ready)),
                )
                self._pending[connection_id] = pending
        return await pending.ready

    async def _own_session(
        self, connection: McpConnectionRow, ready: asyncio.Future[_LiveSession]
    ) -> None:
        """Own one connection's client context for its whole life, in this one task.

        Entering and exiting happen here and nowhere else. That is the invariant: an anyio cancel
        scope belongs to the task that entered it, so a transport opened during an Attempt could
        never be closed by the Worker's shutdown task -- which is exactly what made shutdown raise
        for a Worker that had run a single MCP call. The owner is a plain long-lived task, **not** a
        request queue: once the session is live, callers use it concurrently and directly, and no
        lock is held across a `tools/call`.
        """
        connection_id = connection.connection_id
        owner = asyncio.current_task()
        if owner is None:  # pragma: no cover - this coroutine only ever runs inside a task
            raise RuntimeError("a session owner must run inside a task")
        stack = AsyncExitStack()
        closable = asyncio.Event()
        try:
            client = await self._open_client(stack, connection)
        except BaseException as error:
            # Creation failed before anything was published: close what was opened, in this task,
            # and hand every waiting caller the same failure.
            await stack.aclose()
            self._pending.pop(connection_id, None)
            if not ready.done():
                ready.set_exception(error)
            return
        session = _LiveSession(
            client=client, owner=owner, closable=closable, last_used=self._clock()
        )
        # The owner publishes, not the caller: a caller that is cancelled between the connection
        # succeeding and the cache being written would otherwise strand a live client that a later
        # call could not find and that nothing would ever close.
        self._sessions[connection_id] = session
        self._pending.pop(connection_id, None)
        if not ready.done():
            ready.set_result(session)
        try:
            await closable.wait()
        finally:
            # Only the session this owner published, and only if it is still the cached one: a
            # replacement may already have superseded it.
            if self._sessions.get(connection_id) is session:
                del self._sessions[connection_id]
            await stack.aclose()

    async def _close(self, session: _LiveSession) -> None:
        """Ask a session's owner to exit its context, then wait for it to have done so.

        The context is exited by the owner task and never by the caller, which is what makes
        closing a session created during an Attempt safe from the Worker's shutdown task. A
        failure during teardown is surfaced rather than swallowed, but it never leaves the close
        unfinished: the owner's own `finally` is what releases the transport.
        """
        session.closable.set()
        await session.owner

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

        The cache entry is dropped **before** the close is awaited, so invalidation wins
        immediately: no call made after this point can obtain the session being torn down, however
        long the owner takes to release its transport.

        Sessions are closed **newest first**. LIFO mattered when every client's cancel scope nested
        in one calling task; each session now has its own owner task, so what remains is the same
        ordering the shutdown path has always used, kept so the observable order does not change.
        Dropping the newer sessions first is the conservative direction: the cache is an
        optimization and never authority, so the worst case is one reconnect.
        """
        if connection_id not in self._sessions:
            return
        while self._sessions:
            newest = next(reversed(self._sessions))
            session = self._sessions.pop(newest)
            await self._close(session)
            if newest == connection_id:
                return

    async def close_all(self) -> None:
        """Close every cached session, newest first. Called at Worker shutdown.

        This is the authoritative shutdown path, and it is the reason the owner task exists: a
        session created during an Attempt is owned by a task the Worker's shutdown task is not, so
        closing it here can only work by asking that owner to exit its own context.
        """
        while self._sessions:
            await self.invalidate(next(reversed(self._sessions)))


__all__ = ["IDLE_EXPIRY_SECONDS", "McpGateway"]
