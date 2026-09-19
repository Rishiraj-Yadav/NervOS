"""The durable MCP connection aggregate: its states, its reads, and its deletion predicate.

A connection is *configuration*, never a live client (ADR 0016). This module owns the vocabulary and
the persistence port for that configuration, and deliberately knows nothing about the MCP SDK, the
network, a credential value, or a process.

Three rules shape it:

**The states are the five 0007 already permits.** ``connected``, ``unavailable``, ``needs_refresh``,
``definition_changed`` and ``disabled`` are the whole set; D5 adds no sixth and needs no migration.

**A connection that has not proven itself offers nothing.** The initial state is ``needs_refresh``
rather than ``connected``, and re-enabling returns to ``needs_refresh`` rather than restoring the
previous verdict -- so flipping ``enabled`` back on cannot make a stale catalog executable again.

**Deletion is narrow and refuses rather than cascades.** A connection with audit history, or one
that a nonterminal Run's grants could still reach, is not deleted. There is no history cascade and
no foreign-key bypass.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from nervos_core.application.tool_registry import ToolDefinitionMaterial


class ConnectionTransport(StrEnum):
    """The two transports 0007 permits, and the one target each requires."""

    STDIO = "stdio"
    HTTP = "http"


class ConnectionStatus(StrEnum):
    """The five durable catalog states, exactly as ``0007`` CHECK-constrains them."""

    CONNECTED = "connected"
    UNAVAILABLE = "unavailable"
    NEEDS_REFRESH = "needs_refresh"
    DEFINITION_CHANGED = "definition_changed"
    DISABLED = "disabled"


class DeleteOutcome(StrEnum):
    """The result of a hard delete, including the two refusals."""

    DELETED = "deleted"
    REFUSED_HAS_HISTORY = "refused_has_history"
    REFUSED_LIVE_RUN = "refused_live_run"
    NOT_FOUND = "not_found"


class InvalidMcpConnection(ValueError):
    """A connection request that the durable shape cannot represent."""


@dataclass(frozen=True, slots=True)
class McpConnectionRow:
    """One durable connection row, as the application layer sees it.

    ``credential_ref`` is an alias, never a value, and this dataclass therefore has no field that
    could hold one.
    """

    connection_id: int
    owner_user_id: int
    display_name: str
    transport: ConnectionTransport
    endpoint: str | None
    server_key: str | None
    credential_ref: str | None
    enabled: bool
    catalog_status: ConnectionStatus
    last_discovery_at: datetime | None
    last_error_code: str | None
    last_error_message: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def target(self) -> str:
        """The identifier a credential alias must be bound to for this connection.

        An HTTP connection binds to its origin; a stdio connection binds to its operator-declared
        server key. One accessor means the alias check has exactly one implementation.
        """
        if self.transport is ConnectionTransport.HTTP:
            return self.endpoint or ""
        return self.server_key or ""

    @property
    def can_contribute_tools(self) -> bool:
        """Whether this connection's definitions may currently be offered to a model.

        A disabled connection contributes nothing, and neither does one whose catalog has not been
        proven. This is a cheap local read used only to decide which source refs to synchronise; it
        is never the authority -- D2's live evaluator is.
        """
        return self.enabled and self.catalog_status is not ConnectionStatus.DISABLED


class McpConnectionPersistence(Protocol):
    """Durable reads and writes for connection configuration. Owner-scoped throughout.

    Every method that can name a foreign row either filters by owner or returns the outcome that
    says the row is not there, because foreign and missing are deliberately indistinguishable.
    """

    def create(
        self,
        *,
        owner_user_id: int,
        display_name: str,
        transport: ConnectionTransport,
        endpoint: str | None,
        server_key: str | None,
        credential_ref: str | None,
        now: datetime,
    ) -> int: ...

    def get(self, *, connection_id: int, owner_user_id: int) -> McpConnectionRow | None: ...

    def get_by_id(self, connection_id: int) -> McpConnectionRow | None:
        """Read one row without an owner filter.

        This exists for the Worker's execution path alone. A Worker serves every owner and has no
        owner identity to scope a read with; the authority over whether a call may run is D2's live
        evaluator, which reads the Run's own owner from durable state, so this read cannot widen
        anyone's access. The owner-scoped :meth:`get` remains the only read the API uses.
        """
        ...

    def list_for_owner(
        self, *, owner_user_id: int, limit: int, before_id: int | None
    ) -> tuple[McpConnectionRow, ...]: ...

    def list_contributing_ids(self, *, owner_user_id: int) -> tuple[int, ...]: ...

    def list_all_contributing_ids(self) -> tuple[int, ...]:
        """Every connection id that could currently contribute a tool, for any owner.

        This is the Worker's synchronisation read. A Worker serves every owner and has no owner
        identity to scope a read with, so it registers a local source for each of these and lets
        D2's live evaluator decide, per Run, whether any descriptor behind one may actually be
        offered. Registering a source is not authority: it only makes descriptors *gatherable*, and
        a Run's catalog is still filtered by grants the Run's own Agent Instance holds.
        """
        ...

    def set_display_name(
        self, *, connection_id: int, owner_user_id: int, display_name: str, now: datetime
    ) -> bool: ...

    def set_disabled(self, *, connection_id: int, owner_user_id: int, now: datetime) -> bool: ...

    def set_reenabled(self, *, connection_id: int, owner_user_id: int, now: datetime) -> bool: ...

    def record_discovery_success(
        self,
        *,
        connection_id: int,
        owner_user_id: int,
        status: ConnectionStatus,
        materials: Sequence[ToolDefinitionMaterial],
        now: datetime,
    ) -> bool:
        """Reconcile a completed discovery and mark the connection, in one transaction.

        This is the single atomic step that turns discovered *material* into durable authority:
        definitions are inserted, updated or marked unavailable, and the connection's catalog status
        and discovery timestamp move with them. Splitting it into two commits would let a crash
        expose a `connected` connection whose catalog had not been reconciled yet.
        """
        ...

    def record_discovery_failure(
        self,
        *,
        connection_id: int,
        owner_user_id: int,
        error_code: str,
        error_message: str,
        now: datetime,
    ) -> bool: ...

    def delete(self, *, connection_id: int, owner_user_id: int, now: datetime) -> DeleteOutcome: ...


__all__ = [
    "ConnectionStatus",
    "ConnectionTransport",
    "DeleteOutcome",
    "InvalidMcpConnection",
    "McpConnectionPersistence",
    "McpConnectionRow",
]
