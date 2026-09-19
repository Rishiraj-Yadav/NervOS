"""Durable MCP connection persistence, and the atomic reconciliation of a discovered catalog.

Two responsibilities live here because they share one transactional boundary and one schema:

* the owner-scoped CRUD and lifecycle of a connection row;
* the reconciliation that turns a completed discovery into durable tool definitions.

They are together because reconciliation is not a second step after a status update -- it *is* the
status update. Marking a connection `connected` and writing its definitions in two commits would let
a crash between them publish a connection whose catalog had not been reconciled, and the whole point
of the fail-closed design is that no reader ever sees half a verdict.

**No network, process, or await happens inside a transaction here.** Discovery happens entirely
before this module is called; everything below is a bounded read or write against SQLite.

**This module never touches `agent_tool_grants` except to delete a connection's own rows during a
hard delete.** A grant is authority, and a changed fingerprint is *supposed* to leave an existing
grant drifted and ineffective -- that is the fail-closed signal a user re-confirms. Rewriting one
here would silently re-authorize a tool the user had not re-reviewed.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, TypeVar

from sqlalchemy import Engine, delete, exists, insert, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from nervos_core.application.errors import PersistenceUnavailable
from nervos_core.application.mcp_connections import (
    ConnectionStatus,
    ConnectionTransport,
    DeleteOutcome,
    McpConnectionRow,
)
from nervos_core.application.tool_registry import ToolDefinitionMaterial
from nervos_core.domain.tools import DefinitionStatus, ToolSourceKind
from nervos_core.infrastructure.database.models import (
    AgentInstanceRecord,
    AgentToolGrantRecord,
    McpConnectionRecord,
    RunRecord,
    ToolDefinitionRecord,
    ToolInvocationRecord,
)

_T = TypeVar("_T")
_TRANSACTION_ATTEMPTS = 3

# The terminal Run states. A Run in any other state may still reach a tool of this connection, so a
# connection one of them could touch is not deletable.
_NONTERMINAL_RUN_STATUSES = ("created", "running")

_CONNECTION_COLUMNS = (
    McpConnectionRecord.id,
    McpConnectionRecord.owner_user_id,
    McpConnectionRecord.display_name,
    McpConnectionRecord.transport,
    McpConnectionRecord.endpoint,
    McpConnectionRecord.server_key,
    McpConnectionRecord.credential_ref,
    McpConnectionRecord.enabled,
    McpConnectionRecord.catalog_status,
    McpConnectionRecord.last_discovery_at,
    McpConnectionRecord.last_error_code,
    McpConnectionRecord.last_error_message,
    McpConnectionRecord.created_at,
    McpConnectionRecord.updated_at,
)

_DEFINITION_COLUMNS = (
    ToolDefinitionRecord.id,
    ToolDefinitionRecord.upstream_name,
    ToolDefinitionRecord.model_name,
    ToolDefinitionRecord.display_name,
    ToolDefinitionRecord.description,
    ToolDefinitionRecord.input_schema,
    ToolDefinitionRecord.output_schema,
    ToolDefinitionRecord.fingerprint,
    ToolDefinitionRecord.status,
    ToolDefinitionRecord.hint_read_only,
    ToolDefinitionRecord.hint_destructive,
    ToolDefinitionRecord.hint_idempotent,
    ToolDefinitionRecord.hint_open_world,
)


def _is_contention(error: SQLAlchemyError) -> bool:
    """True if the error is provably a busy/locked failure rather than any other database error."""
    message = str(error).lower()
    return "locked" in message or "busy" in message


class _TransactionRunner:
    """One short BEGIN IMMEDIATE transaction per operation."""

    def __init__(self, engine: Engine, sleep: Callable[[float], None] = time.sleep) -> None:
        self._engine = engine
        self._sleep = sleep

    def run(
        self,
        operation: Callable[[Connection], _T],
        *,
        attempts: int = _TRANSACTION_ATTEMPTS,
    ) -> _T:
        last_error: SQLAlchemyError | None = None
        for index in range(attempts):
            connection = self._engine.connect()
            try:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                result = operation(connection)
                connection.commit()
                return result
            except SQLAlchemyError as error:
                with contextlib.suppress(SQLAlchemyError):
                    connection.rollback()
                if not _is_contention(error):
                    raise PersistenceUnavailable from error
                last_error = error
            except BaseException:
                with contextlib.suppress(SQLAlchemyError):
                    connection.rollback()
                raise
            finally:
                connection.close()

            if index < attempts - 1:
                self._sleep(0.010 * (2**index))

        raise PersistenceUnavailable from last_error


def _from_row(row: Any) -> McpConnectionRow:
    return McpConnectionRow(
        connection_id=int(row.id),
        owner_user_id=int(row.owner_user_id),
        display_name=row.display_name,
        transport=ConnectionTransport(row.transport),
        endpoint=row.endpoint,
        server_key=row.server_key,
        credential_ref=row.credential_ref,
        enabled=bool(row.enabled),
        catalog_status=ConnectionStatus(row.catalog_status),
        last_discovery_at=row.last_discovery_at,
        last_error_code=row.last_error_code,
        last_error_message=row.last_error_message,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyMcpConnectionPersistence:
    """Durable connection configuration over the accepted D1 schema."""

    def __init__(self, engine: Engine, sleep: Callable[[float], None] = time.sleep) -> None:
        self._engine = engine
        self._runner = _TransactionRunner(engine, sleep)

    # --- reads ---------------------------------------------------------------------------------

    def get(self, *, connection_id: int, owner_user_id: int) -> McpConnectionRow | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(*_CONNECTION_COLUMNS).where(
                    McpConnectionRecord.id == connection_id,
                    McpConnectionRecord.owner_user_id == owner_user_id,
                )
            ).one_or_none()
        return None if row is None else _from_row(row)

    def get_by_id(self, connection_id: int) -> McpConnectionRow | None:
        """Read one row without an owner filter, for the Worker's execution path only."""
        with self._engine.connect() as connection:
            row = connection.execute(
                select(*_CONNECTION_COLUMNS).where(McpConnectionRecord.id == connection_id)
            ).one_or_none()
        return None if row is None else _from_row(row)

    def list_for_owner(
        self, *, owner_user_id: int, limit: int, before_id: int | None
    ) -> tuple[McpConnectionRow, ...]:
        statement = select(*_CONNECTION_COLUMNS).where(
            McpConnectionRecord.owner_user_id == owner_user_id
        )
        if before_id is not None:
            statement = statement.where(McpConnectionRecord.id < before_id)
        statement = statement.order_by(McpConnectionRecord.id.desc()).limit(limit)
        with self._engine.connect() as connection:
            rows = connection.execute(statement).all()
        return tuple(_from_row(row) for row in rows)

    def _contributing_ids(
        self, connection: Connection, *, owner_user_id: int | None
    ) -> tuple[int, ...]:
        statement = select(McpConnectionRecord.id).where(
            McpConnectionRecord.enabled.is_(True),
            McpConnectionRecord.catalog_status != ConnectionStatus.DISABLED.value,
        )
        if owner_user_id is not None:
            statement = statement.where(McpConnectionRecord.owner_user_id == owner_user_id)
        return tuple(int(value) for value in connection.execute(statement).scalars().all())

    def list_contributing_ids(self, *, owner_user_id: int) -> tuple[int, ...]:
        with self._engine.connect() as connection:
            return self._contributing_ids(connection, owner_user_id=owner_user_id)

    def list_all_contributing_ids(self) -> tuple[int, ...]:
        with self._engine.connect() as connection:
            return self._contributing_ids(connection, owner_user_id=None)

    # --- lifecycle writes ----------------------------------------------------------------------

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
    ) -> int:
        """Insert one connection. It starts `needs_refresh`, so it offers nothing until proven."""

        def operation(connection: Connection) -> int:
            row = connection.execute(
                insert(McpConnectionRecord).values(
                    owner_user_id=owner_user_id,
                    display_name=display_name,
                    transport=transport.value,
                    endpoint=endpoint,
                    server_key=server_key,
                    credential_ref=credential_ref,
                    enabled=True,
                    catalog_status=ConnectionStatus.NEEDS_REFRESH.value,
                    last_discovery_at=None,
                    last_error_code=None,
                    last_error_message=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            inserted = row.inserted_primary_key
            if inserted is None or inserted[0] is None:  # pragma: no cover - always reported
                raise PersistenceUnavailable
            return int(inserted[0])

        return self._runner.run(operation)

    def set_display_name(
        self, *, connection_id: int, owner_user_id: int, display_name: str, now: datetime
    ) -> bool:
        """Change the one field a connection permits changing."""

        def operation(connection: Connection) -> bool:
            result = connection.execute(
                update(McpConnectionRecord)
                .where(
                    McpConnectionRecord.id == connection_id,
                    McpConnectionRecord.owner_user_id == owner_user_id,
                )
                .values(display_name=display_name, updated_at=now)
            )
            return result.rowcount == 1

        return self._runner.run(operation)

    def set_disabled(self, *, connection_id: int, owner_user_id: int, now: datetime) -> bool:
        """Disable a connection. D2 denies it from the next permission check onward."""

        def operation(connection: Connection) -> bool:
            result = connection.execute(
                update(McpConnectionRecord)
                .where(
                    McpConnectionRecord.id == connection_id,
                    McpConnectionRecord.owner_user_id == owner_user_id,
                )
                .values(
                    enabled=False,
                    catalog_status=ConnectionStatus.DISABLED.value,
                    updated_at=now,
                )
            )
            return result.rowcount == 1

        return self._runner.run(operation)

    def set_reenabled(self, *, connection_id: int, owner_user_id: int, now: datetime) -> bool:
        """Re-enable a connection, discarding its previous verdict.

        The catalog returns to `needs_refresh` and every definition is marked unavailable in the
        same transaction. Without that second half, `enabled=true` alone would make a stale catalog
        executable again -- the flag would be the authority instead of a successful discovery.
        """

        def operation(connection: Connection) -> bool:
            result = connection.execute(
                update(McpConnectionRecord)
                .where(
                    McpConnectionRecord.id == connection_id,
                    McpConnectionRecord.owner_user_id == owner_user_id,
                )
                .values(
                    enabled=True,
                    catalog_status=ConnectionStatus.NEEDS_REFRESH.value,
                    last_error_code=None,
                    last_error_message=None,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                return False
            self._mark_definitions_unavailable(connection, connection_id, now=now)
            return True

        return self._runner.run(operation)

    # --- discovery outcomes --------------------------------------------------------------------

    def record_discovery_success(
        self,
        *,
        connection_id: int,
        owner_user_id: int,
        status: ConnectionStatus,
        materials: Sequence[ToolDefinitionMaterial],
        now: datetime,
    ) -> bool:
        """Reconcile a discovered catalog and mark the connection, atomically."""

        def operation(connection: Connection) -> bool:
            row = connection.execute(
                select(McpConnectionRecord.id).where(
                    McpConnectionRecord.id == connection_id,
                    McpConnectionRecord.owner_user_id == owner_user_id,
                )
            ).one_or_none()
            if row is None:
                return False
            self._reconcile(connection, connection_id, materials, now=now)
            connection.execute(
                update(McpConnectionRecord)
                .where(McpConnectionRecord.id == connection_id)
                .values(
                    catalog_status=status.value,
                    last_discovery_at=now,
                    last_error_code=None,
                    last_error_message=None,
                    updated_at=now,
                )
            )
            return True

        return self._runner.run(operation)

    def record_discovery_failure(
        self,
        *,
        connection_id: int,
        owner_user_id: int,
        error_code: str,
        error_message: str,
        now: datetime,
    ) -> bool:
        """Mark a connection unavailable and withdraw its definitions, atomically."""

        def operation(connection: Connection) -> bool:
            result = connection.execute(
                update(McpConnectionRecord)
                .where(
                    McpConnectionRecord.id == connection_id,
                    McpConnectionRecord.owner_user_id == owner_user_id,
                )
                .values(
                    catalog_status=ConnectionStatus.UNAVAILABLE.value,
                    last_error_code=error_code,
                    last_error_message=error_message,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                return False
            self._mark_definitions_unavailable(connection, connection_id, now=now)
            return True

        return self._runner.run(operation)

    def mark_definition_changed(
        self, *, connection_id: int, owner_user_id: int, now: datetime
    ) -> bool:
        """Record that a reconciled catalog drifted from what a user reviewed."""

        def operation(connection: Connection) -> bool:
            result = connection.execute(
                update(McpConnectionRecord)
                .where(
                    McpConnectionRecord.id == connection_id,
                    McpConnectionRecord.owner_user_id == owner_user_id,
                )
                .values(
                    catalog_status=ConnectionStatus.DEFINITION_CHANGED.value,
                    last_discovery_at=now,
                    last_error_code=None,
                    last_error_message=None,
                    updated_at=now,
                )
            )
            return result.rowcount == 1

        return self._runner.run(operation)

    def _mark_definitions_unavailable(
        self, connection: Connection, connection_id: int, *, now: datetime
    ) -> None:
        connection.execute(
            update(ToolDefinitionRecord)
            .where(
                ToolDefinitionRecord.source_kind == ToolSourceKind.MCP.value,
                ToolDefinitionRecord.source_id == connection_id,
                ToolDefinitionRecord.status != DefinitionStatus.UNAVAILABLE.value,
            )
            .values(status=DefinitionStatus.UNAVAILABLE.value, updated_at=now)
        )

    def _reconcile(
        self,
        connection: Connection,
        connection_id: int,
        materials: Sequence[ToolDefinitionMaterial],
        *,
        now: datetime,
    ) -> None:
        """Apply one complete discovered catalog to the connection's durable definitions.

        Identity is `(source_kind='mcp', source_id, upstream_name)`. Material that has not changed
        is not written at all -- not even its timestamp -- so a repeated refresh is a no-op
        rather than a stream of churn a reviewer would have to read past.
        """
        existing = {
            row.upstream_name: row
            for row in connection.execute(
                select(*_DEFINITION_COLUMNS).where(
                    ToolDefinitionRecord.source_kind == ToolSourceKind.MCP.value,
                    ToolDefinitionRecord.source_id == connection_id,
                )
            ).all()
        }
        seen: set[str] = set()
        for material in materials:
            seen.add(material.upstream_name)
            row = existing.get(material.upstream_name)
            if row is None:
                connection.execute(
                    insert(ToolDefinitionRecord).values(
                        source_kind=ToolSourceKind.MCP.value,
                        source_id=connection_id,
                        upstream_name=material.upstream_name,
                        model_name=material.model_name,
                        display_name=material.display_name,
                        description=material.description,
                        input_schema=material.input_schema,
                        output_schema=material.output_schema,
                        fingerprint=material.fingerprint,
                        status=material.status.value,
                        hint_read_only=material.risk_hints.read_only,
                        hint_destructive=material.risk_hints.destructive,
                        hint_idempotent=material.risk_hints.idempotent,
                        hint_open_world=material.risk_hints.open_world,
                        created_at=now,
                        updated_at=now,
                    )
                )
                continue
            if _is_unchanged(row, material):
                continue
            # `model_name` is deliberately not updated: it is the durable name a reviewed grant and
            # every past invocation refer to, and recomputing it would rename a tool under a user
            # who had already approved it.
            connection.execute(
                update(ToolDefinitionRecord)
                .where(ToolDefinitionRecord.id == int(row.id))
                .values(
                    display_name=material.display_name,
                    description=material.description,
                    input_schema=material.input_schema,
                    output_schema=material.output_schema,
                    fingerprint=material.fingerprint,
                    status=material.status.value,
                    hint_read_only=material.risk_hints.read_only,
                    hint_destructive=material.risk_hints.destructive,
                    hint_idempotent=material.risk_hints.idempotent,
                    hint_open_world=material.risk_hints.open_world,
                    updated_at=now,
                )
            )
        gone = [name for name in existing if name not in seen]
        if gone:
            connection.execute(
                update(ToolDefinitionRecord)
                .where(
                    ToolDefinitionRecord.source_kind == ToolSourceKind.MCP.value,
                    ToolDefinitionRecord.source_id == connection_id,
                    ToolDefinitionRecord.upstream_name.in_(gone),
                    ToolDefinitionRecord.status != DefinitionStatus.UNAVAILABLE.value,
                )
                .values(status=DefinitionStatus.UNAVAILABLE.value, updated_at=now)
            )

    # --- hard delete ---------------------------------------------------------------------------

    def delete(self, *, connection_id: int, owner_user_id: int, now: datetime) -> DeleteOutcome:
        """Delete a connection, its definitions and its grants -- if nothing depends on them."""

        del now  # the rows are removed outright; no timestamp is written

        def operation(connection: Connection) -> DeleteOutcome:
            owned = connection.execute(
                select(McpConnectionRecord.id).where(
                    McpConnectionRecord.id == connection_id,
                    McpConnectionRecord.owner_user_id == owner_user_id,
                )
            ).one_or_none()
            if owned is None:
                return DeleteOutcome.NOT_FOUND
            if self._has_invocation_history(connection, connection_id):
                return DeleteOutcome.REFUSED_HAS_HISTORY
            if self._has_live_run(connection, connection_id, owner_user_id=owner_user_id):
                return DeleteOutcome.REFUSED_LIVE_RUN
            definition_ids = select(ToolDefinitionRecord.id).where(
                ToolDefinitionRecord.source_kind == ToolSourceKind.MCP.value,
                ToolDefinitionRecord.source_id == connection_id,
            )
            connection.execute(
                delete(AgentToolGrantRecord).where(
                    AgentToolGrantRecord.tool_definition_id.in_(definition_ids)
                )
            )
            connection.execute(
                delete(ToolDefinitionRecord).where(
                    ToolDefinitionRecord.source_kind == ToolSourceKind.MCP.value,
                    ToolDefinitionRecord.source_id == connection_id,
                )
            )
            connection.execute(
                delete(McpConnectionRecord).where(McpConnectionRecord.id == connection_id)
            )
            return DeleteOutcome.DELETED

        return self._runner.run(operation)

    def _has_invocation_history(self, connection: Connection, connection_id: int) -> bool:
        """Whether any tool invocation already names a definition of this connection.

        This is a cold, owner-triggered control-plane scan: 0007 declares no index on
        ``tool_invocations.tool_definition_id``, and SQLite does not create one for a foreign key.
        Adding one would require a migration, so the predicate is deliberately allowed to scan -- it
        runs once per delete an owner asks for, and never on the execution hot path.
        """
        return bool(
            connection.execute(
                select(
                    exists().where(
                        ToolInvocationRecord.tool_definition_id
                        == ToolDefinitionRecord.id,
                        ToolDefinitionRecord.source_kind == ToolSourceKind.MCP.value,
                        ToolDefinitionRecord.source_id == connection_id,
                    )
                )
            ).scalar()
        )

    def _has_live_run(
        self, connection: Connection, connection_id: int, *, owner_user_id: int
    ) -> bool:
        """Whether a nonterminal Run's Agent Instance holds a grant to one of its tools."""
        granted = select(AgentToolGrantRecord.id).where(
            AgentToolGrantRecord.agent_instance_id == RunRecord.agent_instance_id,
            AgentToolGrantRecord.tool_definition_id == ToolDefinitionRecord.id,
            ToolDefinitionRecord.source_kind == ToolSourceKind.MCP.value,
            ToolDefinitionRecord.source_id == connection_id,
        )
        return bool(
            connection.execute(
                select(
                    exists().where(
                        RunRecord.agent_instance_id == AgentInstanceRecord.id,
                        AgentInstanceRecord.owner_user_id == owner_user_id,
                        RunRecord.status.in_(_NONTERMINAL_RUN_STATUSES),
                        exists(granted),
                    )
                )
            ).scalar()
        )


def _is_unchanged(row: Any, material: ToolDefinitionMaterial) -> bool:
    """Whether a persisted definition already says exactly what discovery just reported."""
    return (
        row.display_name == material.display_name
        and row.description == material.description
        and row.input_schema == material.input_schema
        and row.output_schema == material.output_schema
        and row.fingerprint == material.fingerprint
        and row.status == material.status.value
        and bool(row.hint_read_only) == material.risk_hints.read_only
        and bool(row.hint_destructive) == material.risk_hints.destructive
        and bool(row.hint_idempotent) == material.risk_hints.idempotent
        and bool(row.hint_open_world) == material.risk_hints.open_world
    )


__all__ = ["SqlAlchemyMcpConnectionPersistence"]
