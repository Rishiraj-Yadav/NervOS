"""Durable tool-definition persistence.

This is the only module that writes ``tool_definitions``. It implements the
:class:`~nervos_core.application.tool_registry.ToolDefinitionPersistence` protocol and depends
on the application layer, never the other way round.

Every mutation runs through one short ``BEGIN IMMEDIATE`` transaction on its own connection,
matching the durable-execution modules. The runner is defined here rather than imported from a
sibling: the same small shape already exists per module in this package, and a private
cross-module import would couple two unrelated aggregates.

**This module never touches ``agent_tool_grants``.** Authority over a definition is a grant,
and grant lifecycle belongs to D2's module. Reconciliation changing a fingerprint is *supposed*
to make an existing grant ineffective -- fail-closed, through the evaluator -- not to rewrite it.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar

from sqlalchemy import ColumnElement, Engine, insert, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from nervos_core.application.errors import PersistenceUnavailable
from nervos_core.application.tool_registry import (
    PersistedToolDefinition,
    ToolDefinitionMaterial,
)
from nervos_core.domain.tools import (
    DefinitionStatus,
    RiskHints,
    ToolSourceKind,
    ToolSourceRef,
)
from nervos_core.infrastructure.database.models import ToolDefinitionRecord

_T = TypeVar("_T")
_TRANSACTION_ATTEMPTS = 3

_COLUMNS = (
    ToolDefinitionRecord.id,
    ToolDefinitionRecord.source_kind,
    ToolDefinitionRecord.source_id,
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


class SqlAlchemyToolDefinitionPersistence:
    """Durable tool-definition reads and writes over the accepted D1 schema."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._runner = _TransactionRunner(engine)

    def find_builtin(self, *, upstream_name: str) -> PersistedToolDefinition | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(*_COLUMNS).where(
                    ToolDefinitionRecord.source_kind == ToolSourceKind.BUILTIN.value,
                    ToolDefinitionRecord.upstream_name == upstream_name,
                )
            ).one_or_none()
        return None if row is None else _from_row(row)

    def insert(self, material: ToolDefinitionMaterial, *, now: datetime) -> int:
        return self._runner.run(lambda connection: self._insert_once(connection, material, now=now))

    def update_material(
        self,
        tool_definition_id: int,
        *,
        material: ToolDefinitionMaterial,
        now: datetime,
    ) -> None:
        self._runner.run(
            lambda connection: self._update_material_once(
                connection, tool_definition_id, material=material, now=now
            )
        )

    def get(self, tool_definition_id: int) -> PersistedToolDefinition | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(*_COLUMNS).where(ToolDefinitionRecord.id == tool_definition_id)
            ).one_or_none()
        return None if row is None else _from_row(row)

    def list_for_source(self, *, source_ref: ToolSourceRef) -> tuple[PersistedToolDefinition, ...]:
        condition = _source_condition(source_ref)
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(*_COLUMNS).where(condition).order_by(ToolDefinitionRecord.id)
            ).all()
        return tuple(_from_row(row) for row in rows)

    def _insert_once(
        self, connection: Connection, material: ToolDefinitionMaterial, *, now: datetime
    ) -> int:
        result = connection.execute(
            insert(ToolDefinitionRecord).values(
                source_kind=material.source_kind.value,
                source_id=material.source_id,
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
        inserted = result.inserted_primary_key
        if inserted is None or inserted[0] is None:
            raise RuntimeError("failed to insert tool definition")
        return int(inserted[0])

    def _update_material_once(
        self,
        connection: Connection,
        tool_definition_id: int,
        *,
        material: ToolDefinitionMaterial,
        now: datetime,
    ) -> None:
        # `model_name` is deliberately absent from this statement. The durable model-facing identity
        # is minted once, at insert, and is never renamed by a later reconciliation: the column
        # cannot be rewritten here even if a caller hands over a different name.
        #
        # `source_kind`, `source_id` and `upstream_name` are absent for the same reason -- they are
        # the definition's identity, and a change to any of them is a NEW definition, not an edit.
        result = connection.execute(
            update(ToolDefinitionRecord)
            .where(ToolDefinitionRecord.id == tool_definition_id)
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
        if result.rowcount != 1:
            raise RuntimeError("failed to update tool definition")


def _source_condition(source_ref: ToolSourceRef) -> ColumnElement[bool]:
    condition: ColumnElement[bool] = (
        ToolDefinitionRecord.source_kind == source_ref.source_kind.value
    )
    if source_ref.source_id is None:
        return condition & ToolDefinitionRecord.source_id.is_(None)
    return condition & (ToolDefinitionRecord.source_id == source_ref.source_id)


def _from_row(row: Any) -> PersistedToolDefinition:
    return PersistedToolDefinition(
        tool_definition_id=int(row.id),
        material=ToolDefinitionMaterial(
            source_kind=ToolSourceKind(row.source_kind),
            source_id=None if row.source_id is None else int(row.source_id),
            upstream_name=row.upstream_name,
            model_name=row.model_name,
            display_name=row.display_name,
            description=row.description,
            input_schema=row.input_schema,
            output_schema=row.output_schema,
            fingerprint=row.fingerprint,
            status=DefinitionStatus(row.status),
            risk_hints=RiskHints(
                read_only=bool(row.hint_read_only),
                destructive=bool(row.hint_destructive),
                idempotent=bool(row.hint_idempotent),
                open_world=bool(row.hint_open_world),
            ),
        ),
    )
