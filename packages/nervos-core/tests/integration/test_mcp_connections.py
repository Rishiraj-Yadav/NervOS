"""D5 durable connection persistence: the reconciliation matrix and the hard-delete predicate.

Everything here runs against a real migrated SQLite database, because the properties that matter are
properties of the *schema*: that a changed fingerprint leaves a reviewed grant drifted rather than
rewritten, that a removed name keeps its row and identity, that a rename mints a new identity with
no inherited authority, and that a delete refuses on durable evidence instead of cascading it away.

The service's decisions over these writes are proved separately with injected doubles; here the
subject is what the durable layer actually commits.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from execution_support import migrate
from nervos_core.application.mcp_connections import (
    ConnectionStatus,
    ConnectionTransport,
    DeleteOutcome,
)
from nervos_core.application.tool_registry import ToolDefinitionMaterial
from nervos_core.domain.runs import STAGE_B_LIMITS
from nervos_core.domain.tools import (
    DefinitionStatus,
    RiskHints,
    ToolSourceKind,
    ToolSourceRef,
    definition_fingerprint,
    model_tool_name,
)
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobExecutionPersistence,
    SqlAlchemyJobPersistence,
)
from nervos_core.infrastructure.database.mcp_connections import (
    SqlAlchemyMcpConnectionPersistence,
)
from nervos_core.infrastructure.database.models import (
    AgentToolGrantRecord,
    ToolDefinitionRecord,
    ToolInvocationRecord,
    UserRecord,
)
from sqlalchemy import Engine, event, insert, select

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
LEASE = timedelta(minutes=2)
ENDPOINT = "https://tools.example.test/mcp"

_DEFINITION_COLUMNS = (
    ToolDefinitionRecord.id,
    ToolDefinitionRecord.source_id,
    ToolDefinitionRecord.upstream_name,
    ToolDefinitionRecord.model_name,
    ToolDefinitionRecord.display_name,
    ToolDefinitionRecord.description,
    ToolDefinitionRecord.input_schema,
    ToolDefinitionRecord.output_schema,
    ToolDefinitionRecord.fingerprint,
    ToolDefinitionRecord.status,
    ToolDefinitionRecord.updated_at,
)


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    """A disposable migrated database holding owner (user 1) and one Agent Instance (id 1)."""
    return migrate(tmp_path / "mcp_connections.db", monkeypatch, agents=1)


@pytest.fixture
def store(engine: Engine) -> SqlAlchemyMcpConnectionPersistence:
    return SqlAlchemyMcpConnectionPersistence(engine)


@pytest.fixture
def other_user(engine: Engine) -> int:
    with engine.begin() as connection:
        connection.execute(
            insert(UserRecord).values(
                username="other",
                password_hash=b"\x00",
                role="admin",
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    with engine.connect() as connection:
        return int(
            connection.execute(
                select(UserRecord.id).where(UserRecord.username == "other")
            ).scalar_one()
        )


def _create(
    store: SqlAlchemyMcpConnectionPersistence,
    *,
    owner_user_id: int = 1,
    display_name: str = "Conn",
    transport: ConnectionTransport = ConnectionTransport.HTTP,
    endpoint: str | None = ENDPOINT,
    server_key: str | None = None,
    now: datetime = NOW,
) -> int:
    return store.create(
        owner_user_id=owner_user_id,
        display_name=display_name,
        transport=transport,
        endpoint=endpoint,
        server_key=server_key,
        credential_ref=None,
        now=now,
    )


def _material(
    connection_id: int,
    upstream_name: str,
    *,
    description: str = "Repeats its input.",
    input_schema: str = '{"type":"object","properties":{}}',
    output_schema: str | None = None,
    status: DefinitionStatus = DefinitionStatus.AVAILABLE,
    risk_hints: RiskHints | None = None,
) -> ToolDefinitionMaterial:
    hints = RiskHints() if risk_hints is None else risk_hints
    model_name = model_tool_name(ToolSourceRef(ToolSourceKind.MCP, connection_id), upstream_name)
    return ToolDefinitionMaterial(
        source_kind=ToolSourceKind.MCP,
        source_id=connection_id,
        upstream_name=upstream_name,
        model_name=model_name,
        display_name=upstream_name,
        description=description,
        input_schema=input_schema,
        output_schema=output_schema,
        fingerprint=definition_fingerprint(
            model_name=model_name,
            upstream_name=upstream_name,
            description=description,
            input_schema=json.loads(input_schema),
            output_schema=None if output_schema is None else json.loads(output_schema),
            source_kind=ToolSourceKind.MCP,
            source_id=connection_id,
            risk_hints=hints,
        ),
        status=status,
        risk_hints=hints,
    )


def _definitions(engine: Engine) -> dict[str, dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(select(*_DEFINITION_COLUMNS)).mappings().all()
    return {str(row["upstream_name"]): dict(row) for row in rows}


def _definition_id(engine: Engine, upstream_name: str) -> int:
    return int(_definitions(engine)[upstream_name]["id"])


def _grant(
    engine: Engine, *, tool_definition_id: int, fingerprint: str, instance_id: int = 1
) -> int:
    with engine.begin() as connection:
        connection.execute(
            insert(AgentToolGrantRecord).values(
                agent_instance_id=instance_id,
                tool_definition_id=tool_definition_id,
                reviewed_fingerprint=fingerprint,
                created_at=NOW,
            )
        )
    with engine.connect() as connection:
        return int(
            connection.execute(
                select(AgentToolGrantRecord.id).where(
                    AgentToolGrantRecord.tool_definition_id == tool_definition_id
                )
            ).scalar_one()
        )


def _grant_row(engine: Engine, grant_id: int) -> dict[str, Any] | None:
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(
                    AgentToolGrantRecord.tool_definition_id,
                    AgentToolGrantRecord.reviewed_fingerprint,
                ).where(AgentToolGrantRecord.id == grant_id)
            )
            .mappings()
            .one_or_none()
        )
    return None if row is None else dict(row)


def _grant_count(engine: Engine, tool_definition_id: int) -> int:
    with engine.connect() as connection:
        return len(
            connection.execute(
                select(AgentToolGrantRecord.id).where(
                    AgentToolGrantRecord.tool_definition_id == tool_definition_id
                )
            ).all()
        )


def _submit(engine: Engine, *, instance_id: int = 1, now: datetime = NOW) -> int:
    run = SqlAlchemyJobPersistence(engine).submit(
        owner_user_id=1,
        agent_instance_id=instance_id,
        input_text="call a tool",
        limits=STAGE_B_LIMITS,
        now=now,
    )
    return run.id


def _claim(engine: Engine, *, now: datetime = NOW) -> Any:
    persistence = SqlAlchemyJobExecutionPersistence(engine)
    claim = persistence.claim_next(
        worker_id="worker-1",
        provider_ids=("anthropic",),
        max_active=4,
        now=now,
        lease_duration=LEASE,
    )
    assert claim is not None
    assert persistence.start_attempt(claim, now=now) is True
    return claim


def _record_invocation(
    engine: Engine, *, claim: Any, connection_id: int, upstream_name: str
) -> None:
    definition = _definitions(engine)[upstream_name]
    with engine.begin() as connection:
        connection.execute(
            insert(ToolInvocationRecord).values(
                run_id=claim.run_id,
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                tool_sequence=1,
                tool_definition_id=int(definition["id"]),
                source_kind=ToolSourceKind.MCP.value,
                source_id=connection_id,
                upstream_name=upstream_name,
                model_name=definition["model_name"],
                definition_fingerprint=definition["fingerprint"],
                status="denied",
                permission_decision="denied_not_granted",
                requested_at=NOW,
                finished_at=NOW,
            )
        )


def _explain_delete(
    engine: Engine, store: SqlAlchemyMcpConnectionPersistence, *, connection_id: int
) -> dict[str, str]:
    """Return the actual ``EXPLAIN QUERY PLAN`` output for each hard-delete refusal predicate."""
    captured: list[tuple[str, Any]] = []

    def capture(
        dbapi_connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        del dbapi_connection, cursor, context, executemany
        captured.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        store.delete(connection_id=connection_id, owner_user_id=1, now=NOW)
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    plans: dict[str, str] = {}
    with engine.connect() as connection:
        for statement, parameters in captured:
            lowered = statement.lower()
            if "tool_invocations" in lowered:
                key = "history"
            elif "from runs" in lowered:
                key = "live_run"
            else:
                continue
            rows = connection.exec_driver_sql(
                "EXPLAIN QUERY PLAN " + statement, parameters
            ).fetchall()
            plans[key] = " | ".join(str(row[-1]) for row in rows)
    return plans


class TestReconciliation:
    def test_a_successful_discovery_inserts_definitions_and_marks_connected(
        self, engine: Engine, store: SqlAlchemyMcpConnectionPersistence
    ) -> None:
        connection_id = _create(store)
        alpha = _material(connection_id, "alpha")
        beta = _material(connection_id, "beta")

        assert (
            store.record_discovery_success(
                connection_id=connection_id,
                owner_user_id=1,
                status=ConnectionStatus.CONNECTED,
                materials=(alpha, beta),
                now=NOW,
            )
            is True
        )

        row = store.get(connection_id=connection_id, owner_user_id=1)
        assert row is not None
        assert row.catalog_status is ConnectionStatus.CONNECTED
        assert row.last_discovery_at == NOW
        assert row.last_error_code is None
        assert row.last_error_message is None
        definitions = _definitions(engine)
        assert set(definitions) == {"alpha", "beta"}
        assert definitions["alpha"]["status"] == DefinitionStatus.AVAILABLE.value
        assert definitions["alpha"]["model_name"] == alpha.model_name
        assert definitions["alpha"]["source_id"] == connection_id

    def test_an_unchanged_catalog_is_not_written_at_all(
        self, engine: Engine, store: SqlAlchemyMcpConnectionPersistence
    ) -> None:
        connection_id = _create(store)
        material = _material(connection_id, "alpha")
        store.record_discovery_success(
            connection_id=connection_id,
            owner_user_id=1,
            status=ConnectionStatus.CONNECTED,
            materials=(material,),
            now=NOW,
        )
        before = _definitions(engine)["alpha"]

        store.record_discovery_success(
            connection_id=connection_id,
            owner_user_id=1,
            status=ConnectionStatus.CONNECTED,
            materials=(material,),
            now=LATER,
        )

        after = _definitions(engine)["alpha"]
        assert after["id"] == before["id"]
        assert after["fingerprint"] == before["fingerprint"]
        # No column update means no `updated_at` churn: the second refresh is a pure read.
        assert after["updated_at"] == before["updated_at"]

    def test_a_changed_material_preserves_model_name_and_leaves_the_grant_drifted(
        self, engine: Engine, store: SqlAlchemyMcpConnectionPersistence
    ) -> None:
        connection_id = _create(store)
        material = _material(connection_id, "alpha")
        store.record_discovery_success(
            connection_id=connection_id,
            owner_user_id=1,
            status=ConnectionStatus.CONNECTED,
            materials=(material,),
            now=NOW,
        )
        tool_definition_id = _definition_id(engine, "alpha")
        grant_id = _grant(
            engine, tool_definition_id=tool_definition_id, fingerprint=material.fingerprint
        )
        changed = _material(connection_id, "alpha", description="Now different.")
        assert changed.fingerprint != material.fingerprint

        store.record_discovery_success(
            connection_id=connection_id,
            owner_user_id=1,
            status=ConnectionStatus.CONNECTED,
            materials=(changed,),
            now=LATER,
        )

        row = _definitions(engine)["alpha"]
        assert row["id"] == tool_definition_id
        assert row["fingerprint"] == changed.fingerprint
        assert row["description"] == "Now different."
        assert row["updated_at"] == LATER
        # The durable model-facing identity is preserved, not recomputed.
        assert row["model_name"] == material.model_name
        # The grant is untouched: drift is the fail-closed signal, not something reconciliation
        # fixes.
        grant = _grant_row(engine, grant_id)
        assert grant is not None
        assert grant["reviewed_fingerprint"] == material.fingerprint

    def test_a_removed_definition_keeps_its_identity_and_is_marked_unavailable(
        self, engine: Engine, store: SqlAlchemyMcpConnectionPersistence
    ) -> None:
        connection_id = _create(store)
        alpha = _material(connection_id, "alpha")
        beta = _material(connection_id, "beta")
        store.record_discovery_success(
            connection_id=connection_id,
            owner_user_id=1,
            status=ConnectionStatus.CONNECTED,
            materials=(alpha, beta),
            now=NOW,
        )
        alpha_id = _definition_id(engine, "alpha")

        store.record_discovery_success(
            connection_id=connection_id,
            owner_user_id=1,
            status=ConnectionStatus.CONNECTED,
            materials=(beta,),
            now=LATER,
        )

        definitions = _definitions(engine)
        assert definitions["alpha"]["id"] == alpha_id
        assert definitions["alpha"]["status"] == DefinitionStatus.UNAVAILABLE.value
        assert definitions["beta"]["status"] == DefinitionStatus.AVAILABLE.value

    def test_a_reappearing_unchanged_definition_is_available_again_with_the_same_fingerprint(
        self, engine: Engine, store: SqlAlchemyMcpConnectionPersistence
    ) -> None:
        connection_id = _create(store)
        material = _material(connection_id, "alpha")
        store.record_discovery_success(
            connection_id=connection_id,
            owner_user_id=1,
            status=ConnectionStatus.CONNECTED,
            materials=(material,),
            now=NOW,
        )
        tool_definition_id = _definition_id(engine, "alpha")
        store.record_discovery_success(
            connection_id=connection_id,
            owner_user_id=1,
            status=ConnectionStatus.CONNECTED,
            materials=(),
            now=LATER,
        )
        assert _definitions(engine)["alpha"]["status"] == DefinitionStatus.UNAVAILABLE.value

        store.record_discovery_success(
            connection_id=connection_id,
            owner_user_id=1,
            status=ConnectionStatus.CONNECTED,
            materials=(material,),
            now=LATER,
        )

        row = _definitions(engine)["alpha"]
        assert row["id"] == tool_definition_id
        assert row["status"] == DefinitionStatus.AVAILABLE.value
        assert row["fingerprint"] == material.fingerprint

    def test_a_reappearing_changed_definition_is_available_again_with_a_new_fingerprint(
        self, engine: Engine, store: SqlAlchemyMcpConnectionPersistence
    ) -> None:
        connection_id = _create(store)
        material = _material(connection_id, "alpha")
        store.record_discovery_success(
            connection_id=connection_id,
            owner_user_id=1,
            status=ConnectionStatus.CONNECTED,
            materials=(material,),
            now=NOW,
        )
        tool_definition_id = _definition_id(engine, "alpha")
        store.record_discovery_success(
            connection_id=connection_id,
            owner_user_id=1,
            status=ConnectionStatus.CONNECTED,
            materials=(),
            now=LATER,
        )
        changed = _material(connection_id, "alpha", description="Different now.")

        store.record_discovery_success(
            connection_id=connection_id,
            owner_user_id=1,
            status=ConnectionStatus.CONNECTED,
            materials=(changed,),
            now=LATER,
        )

        row = _definitions(engine)["alpha"]
        assert row["id"] == tool_definition_id
        assert row["status"] == DefinitionStatus.AVAILABLE.value
        assert row["fingerprint"] == changed.fingerprint

    def test_a_renamed_tool_mints_a_new_identity_that_inherits_no_grant(
        self, engine: Engine, store: SqlAlchemyMcpConnectionPersistence
    ) -> None:
        connection_id = _create(store)
        old = _material(connection_id, "alpha")
        store.record_discovery_success(
            connection_id=connection_id,
            owner_user_id=1,
            status=ConnectionStatus.CONNECTED,
            materials=(old,),
            now=NOW,
        )
        old_id = _definition_id(engine, "alpha")
        grant_id = _grant(engine, tool_definition_id=old_id, fingerprint=old.fingerprint)
        renamed = _material(connection_id, "alpha-renamed")

        store.record_discovery_success(
            connection_id=connection_id,
            owner_user_id=1,
            status=ConnectionStatus.CONNECTED,
            materials=(renamed,),
            now=LATER,
        )

        definitions = _definitions(engine)
        assert definitions["alpha"]["id"] == old_id
        assert definitions["alpha"]["status"] == DefinitionStatus.UNAVAILABLE.value
        new_id = int(definitions["alpha-renamed"]["id"])
        assert new_id != old_id
        assert definitions["alpha-renamed"]["status"] == DefinitionStatus.AVAILABLE.value
        # Authority does not follow a rename: the reviewed grant still names the old definition.
        assert _grant_count(engine, new_id) == 0
        grant = _grant_row(engine, grant_id)
        assert grant is not None
        assert grant["tool_definition_id"] == old_id

    def test_an_unsupported_input_schema_is_stored_exactly_as_discovered(
        self, engine: Engine, store: SqlAlchemyMcpConnectionPersistence
    ) -> None:
        connection_id = _create(store)
        sentinel = '{"type":"nervos-unsupported-input"}'
        material = _material(
            connection_id,
            "weird",
            input_schema=sentinel,
            status=DefinitionStatus.UNSUPPORTED_SCHEMA,
        )

        store.record_discovery_success(
            connection_id=connection_id,
            owner_user_id=1,
            status=ConnectionStatus.CONNECTED,
            materials=(material,),
            now=NOW,
        )

        row = _definitions(engine)["weird"]
        assert row["status"] == DefinitionStatus.UNSUPPORTED_SCHEMA.value
        assert row["input_schema"] == sentinel

    def test_an_unsupported_advertised_output_schema_is_stored_exactly_as_discovered(
        self, engine: Engine, store: SqlAlchemyMcpConnectionPersistence
    ) -> None:
        connection_id = _create(store)
        sentinel = '{"type":"nervos-unsupported-output"}'
        material = _material(
            connection_id,
            "weird-output",
            output_schema=sentinel,
            status=DefinitionStatus.UNSUPPORTED_SCHEMA,
        )

        store.record_discovery_success(
            connection_id=connection_id,
            owner_user_id=1,
            status=ConnectionStatus.CONNECTED,
            materials=(material,),
            now=NOW,
        )

        row = _definitions(engine)["weird-output"]
        assert row["status"] == DefinitionStatus.UNSUPPORTED_SCHEMA.value
        assert row["output_schema"] == sentinel


class TestLifecycle:
    def test_create_starts_enabled_and_needs_refresh(
        self, store: SqlAlchemyMcpConnectionPersistence
    ) -> None:
        connection_id = _create(store)
        row = store.get(connection_id=connection_id, owner_user_id=1)
        assert row is not None
        assert row.enabled is True
        assert row.catalog_status is ConnectionStatus.NEEDS_REFRESH
        assert row.last_discovery_at is None

    def test_reenabling_marks_the_catalog_unavailable(
        self, engine: Engine, store: SqlAlchemyMcpConnectionPersistence
    ) -> None:
        connection_id = _create(store)
        store.record_discovery_success(
            connection_id=connection_id,
            owner_user_id=1,
            status=ConnectionStatus.CONNECTED,
            materials=(_material(connection_id, "alpha"),),
            now=NOW,
        )

        assert store.set_disabled(connection_id=connection_id, owner_user_id=1, now=LATER) is True
        disabled = store.get(connection_id=connection_id, owner_user_id=1)
        assert disabled is not None
        assert disabled.enabled is False
        assert disabled.catalog_status is ConnectionStatus.DISABLED

        assert store.set_reenabled(connection_id=connection_id, owner_user_id=1, now=LATER) is True
        enabled = store.get(connection_id=connection_id, owner_user_id=1)
        assert enabled is not None
        assert enabled.enabled is True
        assert enabled.catalog_status is ConnectionStatus.NEEDS_REFRESH
        # A stale catalog must not become executable merely because `enabled` flipped back on.
        assert _definitions(engine)["alpha"]["status"] == DefinitionStatus.UNAVAILABLE.value

    def test_a_failure_withdraws_the_catalog_and_records_a_safe_pair(
        self, engine: Engine, store: SqlAlchemyMcpConnectionPersistence
    ) -> None:
        connection_id = _create(store)
        store.record_discovery_success(
            connection_id=connection_id,
            owner_user_id=1,
            status=ConnectionStatus.CONNECTED,
            materials=(_material(connection_id, "alpha"),),
            now=NOW,
        )

        assert (
            store.record_discovery_failure(
                connection_id=connection_id,
                owner_user_id=1,
                error_code="mcp_discovery_failed",
                error_message="MCP discovery failed.",
                now=LATER,
            )
            is True
        )

        row = store.get(connection_id=connection_id, owner_user_id=1)
        assert row is not None
        assert row.catalog_status is ConnectionStatus.UNAVAILABLE
        assert row.last_error_code == "mcp_discovery_failed"
        assert row.last_error_message == "MCP discovery failed."
        assert _definitions(engine)["alpha"]["status"] == DefinitionStatus.UNAVAILABLE.value

    def test_update_display_name_is_the_only_mutable_field(
        self, store: SqlAlchemyMcpConnectionPersistence
    ) -> None:
        connection_id = _create(store, display_name="Before")
        assert (
            store.set_display_name(
                connection_id=connection_id,
                owner_user_id=1,
                display_name="After",
                now=LATER,
            )
            is True
        )
        row = store.get(connection_id=connection_id, owner_user_id=1)
        assert row is not None
        assert row.display_name == "After"
        assert row.transport is ConnectionTransport.HTTP
        assert row.endpoint == ENDPOINT
        assert row.server_key is None
        assert row.credential_ref is None

    def test_contributing_ids_exclude_disabled_and_foreign_connections(
        self, store: SqlAlchemyMcpConnectionPersistence, other_user: int
    ) -> None:
        active = _create(store, display_name="Active")
        disabled = _create(store, display_name="Disabled")
        store.set_disabled(connection_id=disabled, owner_user_id=1, now=LATER)
        foreign = _create(store, owner_user_id=other_user, display_name="Foreign")
        pending = _create(store, display_name="Pending")

        # "Contributing" is local eligibility only: enabled and not disabled. A connection that has
        # not been refreshed yet is still gatherable; D2's live evaluator is the authority.
        assert set(store.list_contributing_ids(owner_user_id=1)) == {active, pending}
        assert set(store.list_all_contributing_ids()) == {active, pending, foreign}
        assert disabled not in store.list_all_contributing_ids()

    def test_list_for_owner_is_owner_scoped_and_newest_first(
        self, store: SqlAlchemyMcpConnectionPersistence, other_user: int
    ) -> None:
        first = _create(store, display_name="First")
        second = _create(store, display_name="Second")
        _create(store, owner_user_id=other_user, display_name="Foreign")

        page = store.list_for_owner(owner_user_id=1, limit=10, before_id=None)
        assert [row.connection_id for row in page] == [second, first]
        cursor_page = store.list_for_owner(owner_user_id=1, limit=10, before_id=second)
        assert [row.connection_id for row in cursor_page] == [first]
        assert len(store.list_for_owner(owner_user_id=other_user, limit=10, before_id=None)) == 1

    def test_get_by_id_reads_across_owners_for_the_worker_path(
        self, store: SqlAlchemyMcpConnectionPersistence, other_user: int
    ) -> None:
        foreign = _create(store, owner_user_id=other_user, display_name="Foreign")
        assert store.get(connection_id=foreign, owner_user_id=1) is None
        assert store.get(connection_id=999_999, owner_user_id=1) is None
        assert store.get_by_id(foreign) is not None


class TestOwnerScopedWrites:
    def test_a_foreign_owner_cannot_write_any_field(
        self, store: SqlAlchemyMcpConnectionPersistence, other_user: int
    ) -> None:
        connection_id = _create(store)
        assert (
            store.set_display_name(
                connection_id=connection_id,
                owner_user_id=other_user,
                display_name="Stolen",
                now=LATER,
            )
            is False
        )
        assert (
            store.set_disabled(connection_id=connection_id, owner_user_id=other_user, now=LATER)
            is False
        )
        assert (
            store.set_reenabled(connection_id=connection_id, owner_user_id=other_user, now=LATER)
            is False
        )
        assert (
            store.record_discovery_success(
                connection_id=connection_id,
                owner_user_id=other_user,
                status=ConnectionStatus.CONNECTED,
                materials=(_material(connection_id, "alpha"),),
                now=LATER,
            )
            is False
        )
        assert (
            store.record_discovery_failure(
                connection_id=connection_id,
                owner_user_id=other_user,
                error_code="mcp_discovery_failed",
                error_message="MCP discovery failed.",
                now=LATER,
            )
            is False
        )
        assert (
            store.delete(connection_id=connection_id, owner_user_id=other_user, now=LATER)
            is DeleteOutcome.NOT_FOUND
        )
        row = store.get(connection_id=connection_id, owner_user_id=1)
        assert row is not None
        assert row.display_name == "Conn"
        assert row.enabled is True


class TestHardDelete:
    def test_delete_removes_the_connection_its_definitions_and_its_grants(
        self, engine: Engine, store: SqlAlchemyMcpConnectionPersistence
    ) -> None:
        connection_id = _create(store)
        material = _material(connection_id, "alpha")
        store.record_discovery_success(
            connection_id=connection_id,
            owner_user_id=1,
            status=ConnectionStatus.CONNECTED,
            materials=(material,),
            now=NOW,
        )
        tool_definition_id = _definition_id(engine, "alpha")
        grant_id = _grant(
            engine, tool_definition_id=tool_definition_id, fingerprint=material.fingerprint
        )

        assert (
            store.delete(connection_id=connection_id, owner_user_id=1, now=LATER)
            is DeleteOutcome.DELETED
        )

        assert store.get(connection_id=connection_id, owner_user_id=1) is None
        assert _definitions(engine) == {}
        assert _grant_row(engine, grant_id) is None

    def test_delete_refuses_when_invocation_history_exists(
        self, engine: Engine, store: SqlAlchemyMcpConnectionPersistence
    ) -> None:
        connection_id = _create(store)
        store.record_discovery_success(
            connection_id=connection_id,
            owner_user_id=1,
            status=ConnectionStatus.CONNECTED,
            materials=(_material(connection_id, "alpha"),),
            now=NOW,
        )
        _submit(engine)
        claim = _claim(engine)
        _record_invocation(engine, claim=claim, connection_id=connection_id, upstream_name="alpha")
        assert (
            store.delete(connection_id=connection_id, owner_user_id=1, now=LATER)
            is DeleteOutcome.REFUSED_HAS_HISTORY
        )
        assert store.get(connection_id=connection_id, owner_user_id=1) is not None
        assert _definitions(engine)["alpha"]["status"] == DefinitionStatus.AVAILABLE.value

    def test_delete_refuses_when_a_nonterminal_run_holds_a_grant(
        self, engine: Engine, store: SqlAlchemyMcpConnectionPersistence
    ) -> None:
        connection_id = _create(store)
        material = _material(connection_id, "alpha")
        store.record_discovery_success(
            connection_id=connection_id,
            owner_user_id=1,
            status=ConnectionStatus.CONNECTED,
            materials=(material,),
            now=NOW,
        )
        _grant(
            engine,
            tool_definition_id=_definition_id(engine, "alpha"),
            fingerprint=material.fingerprint,
        )
        _submit(engine)

        assert (
            store.delete(connection_id=connection_id, owner_user_id=1, now=LATER)
            is DeleteOutcome.REFUSED_LIVE_RUN
        )
        assert store.get(connection_id=connection_id, owner_user_id=1) is not None

    def test_delete_leaves_another_connection_untouched(
        self, engine: Engine, store: SqlAlchemyMcpConnectionPersistence
    ) -> None:
        deleted = _create(store, display_name="Deleted")
        kept = _create(store, display_name="Kept")
        store.record_discovery_success(
            connection_id=deleted,
            owner_user_id=1,
            status=ConnectionStatus.CONNECTED,
            materials=(_material(deleted, "alpha"),),
            now=NOW,
        )
        store.record_discovery_success(
            connection_id=kept,
            owner_user_id=1,
            status=ConnectionStatus.CONNECTED,
            materials=(_material(kept, "beta"),),
            now=NOW,
        )

        assert (
            store.delete(connection_id=deleted, owner_user_id=1, now=LATER) is DeleteOutcome.DELETED
        )

        assert store.get(connection_id=kept, owner_user_id=1) is not None
        definitions = _definitions(engine)
        assert set(definitions) == {"beta"}
        assert definitions["beta"]["source_id"] == kept

    def test_delete_is_not_found_for_a_foreign_owner(
        self, engine: Engine, store: SqlAlchemyMcpConnectionPersistence, other_user: int
    ) -> None:
        connection_id = _create(store)
        assert (
            store.delete(connection_id=connection_id, owner_user_id=other_user, now=LATER)
            is DeleteOutcome.NOT_FOUND
        )
        assert store.get(connection_id=connection_id, owner_user_id=1) is not None

    def test_the_refusal_predicates_are_cold_scans(
        self, engine: Engine, store: SqlAlchemyMcpConnectionPersistence
    ) -> None:
        connection_id = _create(store)
        material = _material(connection_id, "alpha")
        store.record_discovery_success(
            connection_id=connection_id,
            owner_user_id=1,
            status=ConnectionStatus.CONNECTED,
            materials=(material,),
            now=NOW,
        )
        _grant(
            engine,
            tool_definition_id=_definition_id(engine, "alpha"),
            fingerprint=material.fingerprint,
        )
        _submit(engine)

        plans = _explain_delete(engine, store, connection_id=connection_id)

        assert "history" in plans and "live_run" in plans
        # 0007 declares no index on `tool_invocations.tool_definition_id`, so the history predicate
        # is an honest cold scan -- it runs once per owner-requested delete, never on the hot path.
        assert "SCAN tool_invocations" in plans["history"]
        assert "SEARCH tool_definitions USING INTEGER PRIMARY KEY" in plans["history"]
        # The live-run predicate is served by the existing `(agent_instance_id, id)` index and the
        # grant unique index; there is no index on a definition id in `runs` to claim.
        assert "SEARCH runs USING INDEX ix_runs_agent_instance_id_id" in plans["live_run"]
