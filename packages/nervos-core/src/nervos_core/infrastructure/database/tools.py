"""Tool grant persistence and permission evaluation.

D2 implements the fail-closed permission layer over the D1 schema.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from datetime import datetime
from typing import TypeVar

from sqlalchemy import Engine, delete, insert, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from nervos_core.application.errors import PersistenceUnavailable
from nervos_core.application.tool_permissions import (
    GrantAlreadyExists,
    McpConnectionNotFound,
    OwnershipMismatch,
    PermissionDecision,
    PermissionDenialReason,
    ToolDefinitionNotFound,
    ToolGrant,
)
from nervos_core.infrastructure.database.models import (
    AgentInstanceRecord,
    AgentToolGrantRecord,
    McpConnectionRecord,
    RunRecord,
    ToolDefinitionRecord,
)

_T = TypeVar("_T")
_TRANSACTION_ATTEMPTS = 3


def _is_contention(error: SQLAlchemyError) -> bool:
    """True if error is provably a busy/locked failure."""
    msg = str(error).lower()
    return "locked" in msg or "busy" in msg


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


class SqlAlchemyToolPermissionPersistence:
    """Durable grant CRUD operations.

    All operations are owner-scoped and transactionally checked.
    Every mutation runs through one short BEGIN IMMEDIATE transaction.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._runner = _TransactionRunner(engine)

    def grant_tool(
        self,
        *,
        owner_user_id: int,
        agent_instance_id: int,
        tool_definition_id: int,
        now: datetime,
    ) -> ToolGrant:
        """Create an ALLOW grant for (agent, tool).

        Idempotent: returns existing grant if fingerprint matches.
        Raises GrantAlreadyExists if fingerprint differs (definition drifted).
        """
        return self._runner.run(
            lambda conn: self._grant_tool_once(
                conn,
                owner_user_id=owner_user_id,
                agent_instance_id=agent_instance_id,
                tool_definition_id=tool_definition_id,
                now=now,
            )
        )

    def _grant_tool_once(
        self,
        conn: Connection,
        *,
        owner_user_id: int,
        agent_instance_id: int,
        tool_definition_id: int,
        now: datetime,
    ) -> ToolGrant:
        # Verify Agent Instance exists and belongs to owner
        agent = conn.execute(
            select(AgentInstanceRecord.id, AgentInstanceRecord.owner_user_id).where(
                AgentInstanceRecord.id == agent_instance_id,
                AgentInstanceRecord.owner_user_id == owner_user_id,
            )
        ).one_or_none()
        if agent is None:
            raise ToolDefinitionNotFound("Agent Instance not found or access denied")

        # Verify Tool Definition exists and check ownership for MCP tools
        definition = conn.execute(
            select(
                ToolDefinitionRecord.id,
                ToolDefinitionRecord.source_kind,
                ToolDefinitionRecord.source_id,
                ToolDefinitionRecord.fingerprint,
            ).where(ToolDefinitionRecord.id == tool_definition_id)
        ).one_or_none()
        if definition is None:
            raise ToolDefinitionNotFound(f"Tool definition {tool_definition_id} not found")

        # For MCP tools, verify connection exists, enabled, and same owner
        if definition.source_kind == "mcp":
            if definition.source_id is None:
                raise ToolDefinitionNotFound("MCP tool has NULL source_id")
            connection = conn.execute(
                select(
                    McpConnectionRecord.id,
                    McpConnectionRecord.owner_user_id,
                    McpConnectionRecord.enabled,
                ).where(McpConnectionRecord.id == definition.source_id)
            ).one_or_none()
            if connection is None:
                raise McpConnectionNotFound(f"MCP connection {definition.source_id} not found")
            if connection.owner_user_id != owner_user_id:
                raise OwnershipMismatch(
                    f"MCP connection {definition.source_id} belongs to different owner"
                )
            if not connection.enabled:
                raise McpConnectionNotFound(f"MCP connection {definition.source_id} is disabled")

        # Check for existing grant
        existing = conn.execute(
            select(
                AgentToolGrantRecord.id,
                AgentToolGrantRecord.reviewed_fingerprint,
                AgentToolGrantRecord.created_at,
            ).where(
                AgentToolGrantRecord.agent_instance_id == agent_instance_id,
                AgentToolGrantRecord.tool_definition_id == tool_definition_id,
            )
        ).one_or_none()

        if existing is not None:
            # Grant exists - check if fingerprint matches
            if existing.reviewed_fingerprint == definition.fingerprint:
                # Idempotent: same grant, same fingerprint
                return ToolGrant(
                    id=existing.id,
                    agent_instance_id=agent_instance_id,
                    tool_definition_id=tool_definition_id,
                    reviewed_fingerprint=existing.reviewed_fingerprint,
                    created_at=existing.created_at,
                )
            else:
                # Definition drifted - caller must revoke first
                raise GrantAlreadyExists(
                    "Grant exists with different fingerprint. "
                    "Definition changed since last review - revoke and re-grant to confirm."
                )

        # Insert new grant
        result = conn.execute(
            insert(AgentToolGrantRecord).values(
                agent_instance_id=agent_instance_id,
                tool_definition_id=tool_definition_id,
                reviewed_fingerprint=definition.fingerprint,
                created_at=now,
            )
        )
        grant_id = result.inserted_primary_key
        if grant_id is None or grant_id[0] is None:
            raise RuntimeError("Failed to insert grant")

        return ToolGrant(
            id=int(grant_id[0]),
            agent_instance_id=agent_instance_id,
            tool_definition_id=tool_definition_id,
            reviewed_fingerprint=definition.fingerprint,
            created_at=now,
        )

    def revoke_tool(
        self,
        *,
        owner_user_id: int,
        agent_instance_id: int,
        tool_definition_id: int,
    ) -> None:
        """Delete the grant row. Idempotent."""
        self._runner.run(
            lambda conn: self._revoke_tool_once(
                conn,
                owner_user_id=owner_user_id,
                agent_instance_id=agent_instance_id,
                tool_definition_id=tool_definition_id,
            )
        )

    def _revoke_tool_once(
        self,
        conn: Connection,
        *,
        owner_user_id: int,
        agent_instance_id: int,
        tool_definition_id: int,
    ) -> None:
        # Verify ownership through Agent Instance
        agent = conn.execute(
            select(AgentInstanceRecord.id).where(
                AgentInstanceRecord.id == agent_instance_id,
                AgentInstanceRecord.owner_user_id == owner_user_id,
            )
        ).one_or_none()
        if agent is None:
            # Idempotent: if agent is missing or not owned, there's no grant to revoke
            return

        # Delete grant - idempotent, succeeds even if no row exists
        conn.execute(
            delete(AgentToolGrantRecord).where(
                AgentToolGrantRecord.agent_instance_id == agent_instance_id,
                AgentToolGrantRecord.tool_definition_id == tool_definition_id,
            )
        )

    def reconfirm_tool(
        self,
        *,
        owner_user_id: int,
        agent_instance_id: int,
        tool_definition_id: int,
        now: datetime,
    ) -> ToolGrant:
        """Reconfirm drifted definition by replacing the grant.

        Atomically: delete old → insert new with new AUTOINCREMENT id.
        """
        return self._runner.run(
            lambda conn: self._reconfirm_tool_once(
                conn,
                owner_user_id=owner_user_id,
                agent_instance_id=agent_instance_id,
                tool_definition_id=tool_definition_id,
                now=now,
            )
        )

    def _reconfirm_tool_once(
        self,
        conn: Connection,
        *,
        owner_user_id: int,
        agent_instance_id: int,
        tool_definition_id: int,
        now: datetime,
    ) -> ToolGrant:
        # Verify Agent Instance
        agent = conn.execute(
            select(AgentInstanceRecord.id, AgentInstanceRecord.owner_user_id).where(
                AgentInstanceRecord.id == agent_instance_id,
                AgentInstanceRecord.owner_user_id == owner_user_id,
            )
        ).one_or_none()
        if agent is None:
            raise ToolDefinitionNotFound("Agent Instance not found or access denied")

        # Verify Tool Definition and get current fingerprint
        definition = conn.execute(
            select(
                ToolDefinitionRecord.id,
                ToolDefinitionRecord.source_kind,
                ToolDefinitionRecord.source_id,
                ToolDefinitionRecord.fingerprint,
            ).where(ToolDefinitionRecord.id == tool_definition_id)
        ).one_or_none()
        if definition is None:
            raise ToolDefinitionNotFound(f"Tool definition {tool_definition_id} not found")

        # For MCP tools, verify connection
        if definition.source_kind == "mcp":
            if definition.source_id is None:
                raise ToolDefinitionNotFound("MCP tool has NULL source_id")
            connection = conn.execute(
                select(
                    McpConnectionRecord.id,
                    McpConnectionRecord.owner_user_id,
                    McpConnectionRecord.enabled,
                ).where(McpConnectionRecord.id == definition.source_id)
            ).one_or_none()
            if connection is None:
                raise McpConnectionNotFound(f"MCP connection {definition.source_id} not found")
            if connection.owner_user_id != owner_user_id:
                raise OwnershipMismatch(
                    f"MCP connection {definition.source_id} belongs to different owner"
                )
            if not connection.enabled:
                raise McpConnectionNotFound(f"MCP connection {definition.source_id} is disabled")

        # Delete old grant
        conn.execute(
            delete(AgentToolGrantRecord).where(
                AgentToolGrantRecord.agent_instance_id == agent_instance_id,
                AgentToolGrantRecord.tool_definition_id == tool_definition_id,
            )
        )

        # Insert new grant with new AUTOINCREMENT id and current fingerprint
        result = conn.execute(
            insert(AgentToolGrantRecord).values(
                agent_instance_id=agent_instance_id,
                tool_definition_id=tool_definition_id,
                reviewed_fingerprint=definition.fingerprint,
                created_at=now,
            )
        )
        grant_id = result.inserted_primary_key
        if grant_id is None or grant_id[0] is None:
            raise RuntimeError("Failed to insert grant")

        return ToolGrant(
            id=int(grant_id[0]),
            agent_instance_id=agent_instance_id,
            tool_definition_id=tool_definition_id,
            reviewed_fingerprint=definition.fingerprint,
            created_at=now,
        )

    def list_grants(
        self,
        *,
        owner_user_id: int,
        agent_instance_id: int,
    ) -> tuple[ToolGrant, ...]:
        """List all current grants for an Agent Instance."""
        return self._runner.run(
            lambda conn: self._list_grants_once(
                conn, owner_user_id=owner_user_id, agent_instance_id=agent_instance_id
            )
        )

    def _list_grants_once(
        self, conn: Connection, *, owner_user_id: int, agent_instance_id: int
    ) -> tuple[ToolGrant, ...]:
        # Verify ownership
        agent = conn.execute(
            select(AgentInstanceRecord.id).where(
                AgentInstanceRecord.id == agent_instance_id,
                AgentInstanceRecord.owner_user_id == owner_user_id,
            )
        ).one_or_none()
        if agent is None:
            return ()

        rows = conn.execute(
            select(
                AgentToolGrantRecord.id,
                AgentToolGrantRecord.agent_instance_id,
                AgentToolGrantRecord.tool_definition_id,
                AgentToolGrantRecord.reviewed_fingerprint,
                AgentToolGrantRecord.created_at,
            ).where(AgentToolGrantRecord.agent_instance_id == agent_instance_id)
        ).all()

        return tuple(
            ToolGrant(
                id=row.id,
                agent_instance_id=row.agent_instance_id,
                tool_definition_id=row.tool_definition_id,
                reviewed_fingerprint=row.reviewed_fingerprint,
                created_at=row.created_at,
            )
            for row in rows
        )

    def list_grants_at_or_before_cutoff(
        self,
        *,
        agent_instance_id: int,
        grant_cutoff_id: int,
    ) -> tuple[ToolGrant, ...]:
        """List one Agent Instance's grant rows whose id is at or below a Run's cutoff.

        A cutoff filter only -- no fingerprint, availability, connection-state or
        ownership evaluation. Catalog assembly must still route each candidate through
        the call-time evaluator. A pure durable read with no ownership pre-flight,
        because the caller already established ownership through the owner-scoped Run
        read that produced the cutoff.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(
                    AgentToolGrantRecord.id,
                    AgentToolGrantRecord.agent_instance_id,
                    AgentToolGrantRecord.tool_definition_id,
                    AgentToolGrantRecord.reviewed_fingerprint,
                    AgentToolGrantRecord.created_at,
                ).where(
                    AgentToolGrantRecord.agent_instance_id == agent_instance_id,
                    AgentToolGrantRecord.id <= grant_cutoff_id,
                )
            ).all()

        return tuple(
            ToolGrant(
                id=row.id,
                agent_instance_id=row.agent_instance_id,
                tool_definition_id=row.tool_definition_id,
                reviewed_fingerprint=row.reviewed_fingerprint,
                created_at=row.created_at,
            )
            for row in rows
        )


class SqlAlchemyToolPermissionEvaluator:
    """Call-time permission evaluator.

    Pure query - no mutations, no caching.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def check_permission(
        self,
        *,
        run_id: int,
        tool_definition_id: int,
    ) -> PermissionDecision:
        """Evaluate whether a tool call is allowed right now.

        Derives Agent Instance, grant cutoff, and definition fingerprint from
        durable rows. Fail-closed: deny unless ALL checks pass.
        """
        with self._engine.connect() as conn:
            # Check 1: Run exists and derive Agent Instance + cutoff from it
            run = conn.execute(
                select(
                    RunRecord.agent_instance_id,
                    RunRecord.tool_grant_cutoff_id,
                ).where(RunRecord.id == run_id)
            ).one_or_none()

            if run is None:
                return PermissionDecision(allowed=False, reason=PermissionDenialReason.NOT_GRANTED)

            agent_instance_id = run.agent_instance_id
            run_tool_grant_cutoff_id = run.tool_grant_cutoff_id

            # Check 2: Grant exists for (run's agent, tool)
            grant = conn.execute(
                select(
                    AgentToolGrantRecord.id,
                    AgentToolGrantRecord.reviewed_fingerprint,
                ).where(
                    AgentToolGrantRecord.agent_instance_id == agent_instance_id,
                    AgentToolGrantRecord.tool_definition_id == tool_definition_id,
                )
            ).one_or_none()

            if grant is None:
                return PermissionDecision(allowed=False, reason=PermissionDenialReason.NOT_GRANTED)

            # Check 3: Grant id <= run's durable cutoff
            if grant.id > run_tool_grant_cutoff_id:
                return PermissionDecision(
                    allowed=False, reason=PermissionDenialReason.GRANT_AFTER_RUN_CUTOFF
                )

            # Check 4: Definition exists and load current fingerprint from DB
            definition = conn.execute(
                select(
                    ToolDefinitionRecord.source_kind,
                    ToolDefinitionRecord.source_id,
                    ToolDefinitionRecord.status,
                    ToolDefinitionRecord.fingerprint,
                ).where(ToolDefinitionRecord.id == tool_definition_id)
            ).one_or_none()

            if definition is None or definition.status != "available":
                return PermissionDecision(
                    allowed=False, reason=PermissionDenialReason.DEFINITION_UNAVAILABLE
                )

            # Check 5: Fingerprint matches (both from durable rows)
            if grant.reviewed_fingerprint != definition.fingerprint:
                return PermissionDecision(
                    allowed=False, reason=PermissionDenialReason.DEFINITION_CHANGED
                )

            # Check 6: For MCP, connection exists and enabled
            if definition.source_kind == "mcp":
                if definition.source_id is None:
                    return PermissionDecision(
                        allowed=False, reason=PermissionDenialReason.DEFINITION_UNAVAILABLE
                    )
                connection = conn.execute(
                    select(McpConnectionRecord.enabled, McpConnectionRecord.owner_user_id).where(
                        McpConnectionRecord.id == definition.source_id
                    )
                ).one_or_none()

                if connection is None or not connection.enabled:
                    return PermissionDecision(
                        allowed=False, reason=PermissionDenialReason.CONNECTION_DISABLED
                    )

                # Check 7: MCP connection owner matches Agent Instance owner
                agent = conn.execute(
                    select(AgentInstanceRecord.owner_user_id).where(
                        AgentInstanceRecord.id == agent_instance_id
                    )
                ).one_or_none()

                if agent is None:
                    return PermissionDecision(
                        allowed=False, reason=PermissionDenialReason.NOT_GRANTED
                    )

                if connection.owner_user_id != agent.owner_user_id:
                    return PermissionDecision(
                        allowed=False, reason=PermissionDenialReason.OWNER_MISMATCH
                    )

            # All checks passed
            return PermissionDecision(allowed=True)
