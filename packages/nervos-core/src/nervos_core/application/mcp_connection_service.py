"""MCP connection management: the owner-scoped operations over durable connection configuration.

A connection is *configuration*, never a live client (ADR 0016). This service owns the operations an
owner may perform on that configuration and nothing else: it creates, reads, renames, enables,
disables, deletes and refreshes. It never opens a socket, never starts a process, and never resolves
a credential *value* -- the credential reference it stores is an alias, and no code path here can
turn that alias into a secret, record one, return one, or log one.

**The transport is immutable by construction.** ``transport``, ``endpoint``, ``server_key`` and
``credential_ref`` identify *where this connection reaches and how*, and every one of them is
decided once, at creation, against operator-owned configuration. There is deliberately no setter for
any of them: a connection's audience cannot be changed by editing a row, so possessing a stored
connection is never enough to spend a credential somewhere new. ``display_name`` is the only mutable
field.

**Discovery is behind a port, so this module stays SDK-neutral.** :class:`McpDiscovery` is the async
boundary through which a discovered catalog arrives; the composition root injects the concrete MCP
implementation. Nothing here imports the MCP SDK, dials the network, or runs a process inside a
database transaction: :meth:`McpConnectionService.refresh` reads the row, closes the transaction,
awaits discovery, and then commits the whole verdict -- status, timestamp, error pair and reconciled
definitions -- in one short transaction.

**Failures become safe state, not exceptions.** A discovery failure is mapped to a bounded,
NervOS-authored error pair and an ``unavailable`` connection; a remote-controlled string never
reaches ``last_error_message``. Only cancellation and database failures propagate.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Protocol

from nervos_core.application.clock import Clock, require_utc
from nervos_core.application.mcp_connections import (
    ConnectionStatus,
    ConnectionTransport,
    DeleteOutcome,
    InvalidMcpConnection,
    McpConnectionPersistence,
    McpConnectionRow,
)
from nervos_core.application.tool_permissions import McpConnectionNotFound
from nervos_core.application.tool_registry import ToolDefinitionMaterial
from nervos_core.domain.tools import MAX_DISPLAY_NAME_BYTES, MAX_DISPLAY_NAME_LENGTH

# The durable shape of an operator-declared name, mirroring the migration's CHECK: lowercase
# kebab, so an environment-variable name -- the credential passthrough ADR 0016 forbids by name --
# cannot be represented in either `server_key` or `credential_ref`.
_DECLARED_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,127}")

_MAX_ENDPOINT_LENGTH = 512
_MAX_ERROR_MESSAGE_LENGTH = 512
_SAFE_ERROR_CODE = re.compile(r"[a-z0-9_]{1,64}")

# The fallback error pair for a failure whose cause this layer cannot classify. It is a fixed
# NervOS-authored sentence: an arbitrary exception's text may quote a remote server, and none of it
# is ever persisted.
_DEFAULT_ERROR_CODE = "mcp_discovery_failed"
_DEFAULT_ERROR_MESSAGE = "MCP discovery failed."


class McpDiscovery(Protocol):
    """Provider-neutral catalog discovery.

    Implementations reach the remote side; this module never does. The port is deliberately a plain
    async callable rather than a method on a connection object so that ``nervos-core`` stays free of
    any MCP SDK import and the composition root can inject the concrete adapter.
    """

    async def __call__(
        self, connection: McpConnectionRow
    ) -> tuple[ToolDefinitionMaterial, ...]: ...


class McpOperator(Protocol):
    """The operator-owned facts a connection is validated against.

    Every method is a predicate: ``True`` means the operator declared this alias binding, this stdio
    server key, or this endpoint origin. The concrete adapter wraps the operator configuration and
    its egress allowlist, and -- for endpoints in particular -- decides purely from configuration,
    so this service still never dials the network. ``endpoint_allowed`` is the injected callable
    that keeps the HTTP check out of the SDK.
    """

    def check_alias_target(self, alias: str, target: str) -> bool: ...

    def stdio_server(self, server_key: str) -> bool: ...

    def endpoint_allowed(self, endpoint: str) -> bool: ...


class McpConnectionService:
    """Owner-scoped management of durable MCP connection configuration."""

    def __init__(
        self,
        persistence: McpConnectionPersistence,
        discovery: McpDiscovery,
        operator: McpOperator,
        clock: Clock,
    ) -> None:
        self._persistence = persistence
        self._discovery = discovery
        self._operator = operator
        self._clock = clock

    # -- creation and reads ------------------------------------------------------------------------

    def create_connection(
        self,
        owner_user_id: int,
        *,
        display_name: str,
        transport: ConnectionTransport,
        endpoint: str | None = None,
        server_key: str | None = None,
        credential_ref: str | None = None,
    ) -> McpConnectionRow:
        """Create one connection, validated against the operator's declared configuration.

        The target and the credential alias are proved together: an alias is only stored when the
        operator has bound it to exactly this target, so a stored row cannot name an audience the
        credential was never allowed to address.
        """
        name = _validated_display_name(display_name)
        target = self._validated_target(transport, endpoint=endpoint, server_key=server_key)
        if credential_ref is not None:
            if _DECLARED_NAME.fullmatch(credential_ref) is None:
                raise InvalidMcpConnection("a credential alias must be a lowercase kebab name")
            if not self._operator.check_alias_target(credential_ref, target):
                raise InvalidMcpConnection("the credential alias is not bound to this target")
        connection_id = self._persistence.create(
            owner_user_id=owner_user_id,
            display_name=name,
            transport=transport,
            endpoint=endpoint,
            server_key=server_key,
            credential_ref=credential_ref,
            now=require_utc(self._clock()),
        )
        return self._require(owner_user_id, connection_id)

    def get_connection(self, owner_user_id: int, connection_id: int) -> McpConnectionRow:
        """Return one owned connection; a foreign or missing row is the same not-found."""
        return self._require(owner_user_id, connection_id)

    def list_connections(
        self, owner_user_id: int, *, limit: int, before_id: int | None = None
    ) -> tuple[McpConnectionRow, ...]:
        """Return one owner-scoped page of newest-first connections."""
        return self._persistence.list_for_owner(
            owner_user_id=owner_user_id, limit=limit, before_id=before_id
        )

    # -- mutation ----------------------------------------------------------------------------------

    def update_display_name(
        self, owner_user_id: int, connection_id: int, display_name: str
    ) -> McpConnectionRow:
        """Rename a connection. This is the only mutable field on the aggregate."""
        name = _validated_display_name(display_name)
        if not self._persistence.set_display_name(
            connection_id=connection_id,
            owner_user_id=owner_user_id,
            display_name=name,
            now=require_utc(self._clock()),
        ):
            raise McpConnectionNotFound(f"MCP connection {connection_id} not found")
        return self._require(owner_user_id, connection_id)

    def enable_connection(self, owner_user_id: int, connection_id: int) -> McpConnectionRow:
        """Re-enable a connection and return it to ``needs_refresh``.

        Definitions contributed before the disable are marked unavailable by the durable write, so
        re-enabling cannot make a stale catalog executable again.
        """
        if not self._persistence.set_reenabled(
            connection_id=connection_id,
            owner_user_id=owner_user_id,
            now=require_utc(self._clock()),
        ):
            raise McpConnectionNotFound(f"MCP connection {connection_id} not found")
        return self._require(owner_user_id, connection_id)

    def disable_connection(self, owner_user_id: int, connection_id: int) -> McpConnectionRow:
        """Disable a connection. It contributes nothing until it is enabled and refreshed again."""
        if not self._persistence.set_disabled(
            connection_id=connection_id,
            owner_user_id=owner_user_id,
            now=require_utc(self._clock()),
        ):
            raise McpConnectionNotFound(f"MCP connection {connection_id} not found")
        return self._require(owner_user_id, connection_id)

    def delete_connection(self, owner_user_id: int, connection_id: int) -> DeleteOutcome:
        """Hard-delete an owned connection, or return why the durable write refused.

        The outcome is returned rather than raised so a caller can distinguish a refusal with
        history from a refusal with a live Run from a connection that is not this owner's.
        """
        return self._persistence.delete(
            connection_id=connection_id, owner_user_id=owner_user_id, now=require_utc(self._clock())
        )

    async def refresh(self, owner_user_id: int, connection_id: int) -> McpConnectionRow:
        """Discover the connection's catalog and commit one verdict for it.

        The row is read owner-scoped and the read transaction is closed **before** discovery runs,
        so no database transaction is ever held across the network. Discovery's result -- the
        reconciled definitions, the timestamp and the status, or a safe error pair -- is committed
        in a single short transaction, so a concurrent reader never sees a half-applied catalog.
        """
        connection = self._require(owner_user_id, connection_id)
        if not connection.enabled:
            raise InvalidMcpConnection("a disabled connection cannot be refreshed")
        now = require_utc(self._clock())
        try:
            materials: Sequence[ToolDefinitionMaterial] = await self._discovery(connection)
        except Exception as error:
            # Every discovery failure must become a durable, safe verdict rather than propagate: the
            # remote detail is not this layer's to interpret, and none of it may be persisted.
            error_code, error_message = _safe_discovery_failure(error)
            recorded = self._persistence.record_discovery_failure(
                connection_id=connection_id,
                owner_user_id=owner_user_id,
                error_code=error_code,
                error_message=error_message,
                now=now,
            )
        else:
            recorded = self._persistence.record_discovery_success(
                connection_id=connection_id,
                owner_user_id=owner_user_id,
                status=ConnectionStatus.CONNECTED,
                now=now,
                materials=materials,
            )
        if not recorded:
            raise McpConnectionNotFound(f"MCP connection {connection_id} not found")
        return self._require(owner_user_id, connection_id)

    # -- helpers -----------------------------------------------------------------------------------

    def _require(self, owner_user_id: int, connection_id: int) -> McpConnectionRow:
        connection = self._persistence.get(connection_id=connection_id, owner_user_id=owner_user_id)
        if connection is None:
            raise McpConnectionNotFound(f"MCP connection {connection_id} not found")
        return connection

    def _validated_target(
        self,
        transport: ConnectionTransport,
        *,
        endpoint: str | None,
        server_key: str | None,
    ) -> str:
        """Validate the one target this transport requires, and return it.

        The target is what a credential alias must be bound to. Validation is entirely against
        operator configuration: an HTTP endpoint is checked against the injected allowlist predicate
        without being contacted, and a stdio key is checked against the operator's declared servers.
        """
        if transport is ConnectionTransport.HTTP:
            if endpoint is None or server_key is not None:
                raise InvalidMcpConnection(
                    "an http connection requires an endpoint and no server key"
                )
            if not 1 <= len(endpoint) <= _MAX_ENDPOINT_LENGTH or "\x00" in endpoint:
                raise InvalidMcpConnection("the endpoint is outside its permitted shape")
            if not self._operator.endpoint_allowed(endpoint):
                raise InvalidMcpConnection("the endpoint origin is not permitted by the operator")
            return endpoint
        if server_key is None or endpoint is not None:
            raise InvalidMcpConnection("a stdio connection requires a server key and no endpoint")
        if _DECLARED_NAME.fullmatch(server_key) is None:
            raise InvalidMcpConnection("a server key must be a lowercase kebab name")
        if not self._operator.stdio_server(server_key):
            raise InvalidMcpConnection("the server key is not declared by the operator")
        return server_key


def _validated_display_name(display_name: str) -> str:
    """Return the trimmed display name, or refuse one the durable shape cannot hold."""
    name = display_name.strip()
    if not 1 <= len(name) <= MAX_DISPLAY_NAME_LENGTH:
        raise InvalidMcpConnection("a display name must be 1..100 characters")
    if len(name.encode("utf-8")) > MAX_DISPLAY_NAME_BYTES:
        raise InvalidMcpConnection("a display name must be at most 400 bytes")
    return name


def _safe_discovery_failure(error: Exception) -> tuple[str, str]:
    """Map a discovery failure to a bounded, persistable error pair.

    A failure that carries its own already-safe classification -- a non-empty ``code`` matching the
    durable alphabet and a bounded ``message`` -- is recorded as such; anything else, including an
    arbitrary exception whose text could quote a remote server, becomes the fixed fallback pair. The
    exception's own text is never persisted.
    """
    code = getattr(error, "code", None)
    message = getattr(error, "message", None)
    if (
        isinstance(code, str)
        and _SAFE_ERROR_CODE.fullmatch(code) is not None
        and isinstance(message, str)
        and 1 <= len(message) <= _MAX_ERROR_MESSAGE_LENGTH
    ):
        return code, message
    return _DEFAULT_ERROR_CODE, _DEFAULT_ERROR_MESSAGE
