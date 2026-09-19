"""D5 service behaviour: validation, fail-closed discovery, and the absence of a transport setter.

Everything here runs against injected doubles, so each test states one rule of the service without a
database. The reconciliation matrix itself is proved against the real schema in the integration
suite; what is proved here is what the service decides *before* it reaches persistence -- which
connection requests are refused, which failures become durable safe state, and that no code path can
mutate where a connection reaches or which credential alias it carries.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from nervos_core.application.mcp_connection_service import McpConnectionService
from nervos_core.application.mcp_connections import (
    ConnectionStatus,
    ConnectionTransport,
    DeleteOutcome,
    InvalidMcpConnection,
    McpConnectionRow,
)
from nervos_core.application.tool_permissions import McpConnectionNotFound
from nervos_core.application.tool_registry import ToolDefinitionMaterial
from nervos_core.domain.tools import DefinitionStatus, RiskHints, ToolSourceKind

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 19, 12, 5, tzinfo=UTC)
ENDPOINT = "https://tools.example.test/mcp"


class FakeStore:
    """A minimal in-memory :class:`McpConnectionPersistence`, recording what it was asked to do."""

    def __init__(self) -> None:
        self.rows: dict[int, McpConnectionRow] = {}
        self.created: list[dict[str, Any]] = []
        self.successes: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.delete_outcome = DeleteOutcome.DELETED
        self._next_id = 1

    def _owned(self, connection_id: int, owner_user_id: int) -> McpConnectionRow | None:
        row = self.rows.get(connection_id)
        return row if row is not None and row.owner_user_id == owner_user_id else None

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
        connection_id = self._next_id
        self._next_id += 1
        self.rows[connection_id] = McpConnectionRow(
            connection_id=connection_id,
            owner_user_id=owner_user_id,
            display_name=display_name,
            transport=transport,
            endpoint=endpoint,
            server_key=server_key,
            credential_ref=credential_ref,
            enabled=True,
            catalog_status=ConnectionStatus.NEEDS_REFRESH,
            last_discovery_at=None,
            last_error_code=None,
            last_error_message=None,
            created_at=now,
            updated_at=now,
        )
        self.created.append(
            {
                "owner_user_id": owner_user_id,
                "display_name": display_name,
                "transport": transport,
                "endpoint": endpoint,
                "server_key": server_key,
                "credential_ref": credential_ref,
            }
        )
        return connection_id

    def get(self, *, connection_id: int, owner_user_id: int) -> McpConnectionRow | None:
        return self._owned(connection_id, owner_user_id)

    def get_by_id(self, connection_id: int) -> McpConnectionRow | None:
        return self.rows.get(connection_id)

    def list_for_owner(
        self, *, owner_user_id: int, limit: int, before_id: int | None
    ) -> tuple[McpConnectionRow, ...]:
        owned = [row for row in self.rows.values() if row.owner_user_id == owner_user_id]
        owned.sort(key=lambda row: row.connection_id, reverse=True)
        return tuple(owned[:limit])

    def list_contributing_ids(self, *, owner_user_id: int) -> tuple[int, ...]:
        return tuple(
            row.connection_id
            for row in self.rows.values()
            if row.owner_user_id == owner_user_id and row.can_contribute_tools
        )

    def list_all_contributing_ids(self) -> tuple[int, ...]:
        return tuple(row.connection_id for row in self.rows.values() if row.can_contribute_tools)

    def set_display_name(
        self, *, connection_id: int, owner_user_id: int, display_name: str, now: datetime
    ) -> bool:
        row = self._owned(connection_id, owner_user_id)
        if row is None:
            return False
        self.rows[connection_id] = replace(row, display_name=display_name, updated_at=now)
        return True

    def set_disabled(self, *, connection_id: int, owner_user_id: int, now: datetime) -> bool:
        row = self._owned(connection_id, owner_user_id)
        if row is None:
            return False
        self.rows[connection_id] = replace(
            row, enabled=False, catalog_status=ConnectionStatus.DISABLED, updated_at=now
        )
        return True

    def set_reenabled(self, *, connection_id: int, owner_user_id: int, now: datetime) -> bool:
        row = self._owned(connection_id, owner_user_id)
        if row is None:
            return False
        self.rows[connection_id] = replace(
            row,
            enabled=True,
            catalog_status=ConnectionStatus.NEEDS_REFRESH,
            updated_at=now,
        )
        return True

    def record_discovery_success(
        self,
        *,
        connection_id: int,
        owner_user_id: int,
        status: ConnectionStatus,
        materials: Sequence[ToolDefinitionMaterial],
        now: datetime,
    ) -> bool:
        row = self._owned(connection_id, owner_user_id)
        if row is None:
            return False
        self.successes.append(
            {
                "connection_id": connection_id,
                "owner_user_id": owner_user_id,
                "status": status,
                "materials": materials,
            }
        )
        self.rows[connection_id] = replace(
            row,
            catalog_status=status,
            last_discovery_at=now,
            last_error_code=None,
            last_error_message=None,
            updated_at=now,
        )
        return True

    def record_discovery_failure(
        self,
        *,
        connection_id: int,
        owner_user_id: int,
        error_code: str,
        error_message: str,
        now: datetime,
    ) -> bool:
        row = self._owned(connection_id, owner_user_id)
        if row is None:
            return False
        self.failures.append(
            {
                "connection_id": connection_id,
                "error_code": error_code,
                "error_message": error_message,
            }
        )
        self.rows[connection_id] = replace(
            row,
            catalog_status=ConnectionStatus.UNAVAILABLE,
            last_error_code=error_code,
            last_error_message=error_message,
            updated_at=now,
        )
        return True

    def mark_definition_changed(
        self, *, connection_id: int, owner_user_id: int, now: datetime
    ) -> bool:
        row = self._owned(connection_id, owner_user_id)
        if row is None:
            return False
        self.rows[connection_id] = replace(
            row, catalog_status=ConnectionStatus.DEFINITION_CHANGED, updated_at=now
        )
        return True

    def delete(self, *, connection_id: int, owner_user_id: int, now: datetime) -> DeleteOutcome:
        if self._owned(connection_id, owner_user_id) is None:
            return DeleteOutcome.NOT_FOUND
        if self.delete_outcome is DeleteOutcome.DELETED:
            del self.rows[connection_id]
        return self.delete_outcome


class FakeOperator:
    def __init__(
        self,
        *,
        aliases: frozenset[tuple[str, str]] = frozenset(),
        servers: frozenset[str] = frozenset(),
        endpoints: frozenset[str] = frozenset(),
    ) -> None:
        self._aliases = aliases
        self._servers = servers
        self._endpoints = endpoints

    def check_alias_target(self, alias: str, target: str) -> bool:
        return (alias, target) in self._aliases

    def stdio_server(self, server_key: str) -> bool:
        return server_key in self._servers

    def endpoint_allowed(self, endpoint: str) -> bool:
        return endpoint in self._endpoints


class FakeDiscovery:
    def __init__(
        self,
        *,
        result: tuple[ToolDefinitionMaterial, ...] = (),
        error: Exception | None = None,
    ) -> None:
        self._result = result
        self._error = error
        self.connections: list[McpConnectionRow] = []

    async def __call__(self, connection: McpConnectionRow) -> tuple[ToolDefinitionMaterial, ...]:
        self.connections.append(connection)
        if self._error is not None:
            raise self._error
        return self._result


class ClassifiedError(Exception):
    """A failure that already carries its own bounded, NervOS-authored code and message."""

    def __init__(self) -> None:
        self.code = "mcp_server_unavailable"
        self.message = "The MCP server could not be reached."


def _clock() -> datetime:
    return NOW


def _service(
    store: FakeStore,
    *,
    discovery: FakeDiscovery | None = None,
    operator: FakeOperator | None = None,
) -> McpConnectionService:
    return McpConnectionService(
        store,
        discovery or FakeDiscovery(),
        operator or FakeOperator(endpoints=frozenset({ENDPOINT})),
        _clock,
    )


def _material(connection_id: int, upstream_name: str = "echo") -> ToolDefinitionMaterial:
    return ToolDefinitionMaterial(
        source_kind=ToolSourceKind.MCP,
        source_id=connection_id,
        upstream_name=upstream_name,
        model_name=f"nervos__c{connection_id}__{upstream_name}__abcdef012345",
        display_name=upstream_name,
        description="Repeats its input.",
        input_schema='{"type":"object"}',
        output_schema=None,
        fingerprint="a" * 64,
        status=DefinitionStatus.AVAILABLE,
        risk_hints=RiskHints(),
    )


class TestCreateConnection:
    def test_a_blank_or_overlong_display_name_is_refused(self) -> None:
        service = _service(FakeStore())
        for name in ("", "   ", "x" * 101):
            with pytest.raises(InvalidMcpConnection):
                service.create_connection(
                    1, display_name=name, transport=ConnectionTransport.HTTP, endpoint=ENDPOINT
                )

    def test_an_http_connection_requires_an_endpoint_and_no_server_key(self) -> None:
        service = _service(FakeStore())
        with pytest.raises(InvalidMcpConnection):
            service.create_connection(1, display_name="A", transport=ConnectionTransport.HTTP)
        with pytest.raises(InvalidMcpConnection):
            service.create_connection(
                1,
                display_name="A",
                transport=ConnectionTransport.HTTP,
                endpoint=ENDPOINT,
                server_key="declared",
            )

    def test_an_endpoint_the_operator_did_not_allowlist_is_refused(self) -> None:
        service = _service(FakeStore())
        with pytest.raises(InvalidMcpConnection):
            service.create_connection(
                1,
                display_name="A",
                transport=ConnectionTransport.HTTP,
                endpoint="https://elsewhere.example.test/mcp",
            )

    def test_a_stdio_connection_requires_a_declared_server_key(self) -> None:
        service = _service(FakeStore(), operator=FakeOperator(servers=frozenset({"declared"})))
        with pytest.raises(InvalidMcpConnection):
            service.create_connection(1, display_name="A", transport=ConnectionTransport.STDIO)
        with pytest.raises(InvalidMcpConnection):
            service.create_connection(
                1, display_name="A", transport=ConnectionTransport.STDIO, server_key="unknown"
            )
        with pytest.raises(InvalidMcpConnection):
            service.create_connection(
                1,
                display_name="A",
                transport=ConnectionTransport.STDIO,
                server_key="NotKebab",
            )

    def test_a_credential_alias_must_be_bound_to_exactly_this_target(self) -> None:
        store = FakeStore()
        service = _service(
            store,
            operator=FakeOperator(
                aliases=frozenset({("tools-key", ENDPOINT)}),
                endpoints=frozenset({ENDPOINT, "https://other.example.test/mcp"}),
            ),
        )
        with pytest.raises(InvalidMcpConnection):
            # The alias is real, but it is bound to a different origin than the one requested.
            service.create_connection(
                1,
                display_name="A",
                transport=ConnectionTransport.HTTP,
                endpoint="https://other.example.test/mcp",
                credential_ref="tools-key",
            )
        with pytest.raises(InvalidMcpConnection):
            service.create_connection(
                1,
                display_name="A",
                transport=ConnectionTransport.HTTP,
                endpoint=ENDPOINT,
                credential_ref="Tools_Key",
            )

    def test_a_well_formed_connection_starts_enabled_and_needs_refresh(self) -> None:
        store = FakeStore()
        service = _service(
            store,
            operator=FakeOperator(
                aliases=frozenset({("tools-key", ENDPOINT)}), endpoints=frozenset({ENDPOINT})
            ),
        )
        row = service.create_connection(
            1,
            display_name="  Tools  ",
            transport=ConnectionTransport.HTTP,
            endpoint=ENDPOINT,
            credential_ref="tools-key",
        )
        assert row.display_name == "Tools"
        assert row.enabled is True
        assert row.catalog_status is ConnectionStatus.NEEDS_REFRESH
        assert row.credential_ref == "tools-key"

    def test_the_transport_can_never_be_mutated_after_creation(self) -> None:
        service = _service(FakeStore())
        for forbidden in (
            "set_transport",
            "set_endpoint",
            "set_server_key",
            "set_credential_ref",
            "update_connection",
        ):
            assert not hasattr(service, forbidden), forbidden


class TestOwnerScoping:
    def test_a_foreign_connection_is_indistinguishable_from_a_missing_one(self) -> None:
        store = FakeStore()
        service = _service(store)
        connection_id = store.create(
            owner_user_id=1,
            display_name="A",
            transport=ConnectionTransport.HTTP,
            endpoint=ENDPOINT,
            server_key=None,
            credential_ref=None,
            now=NOW,
        )
        with pytest.raises(McpConnectionNotFound):
            service.get_connection(2, connection_id)
        with pytest.raises(McpConnectionNotFound):
            service.get_connection(1, connection_id + 999)

    def test_updates_and_deletes_for_a_foreign_owner_are_not_found(self) -> None:
        store = FakeStore()
        service = _service(store)
        connection_id = store.create(
            owner_user_id=1,
            display_name="A",
            transport=ConnectionTransport.HTTP,
            endpoint=ENDPOINT,
            server_key=None,
            credential_ref=None,
            now=NOW,
        )
        with pytest.raises(McpConnectionNotFound):
            service.update_display_name(2, connection_id, "B")
        with pytest.raises(McpConnectionNotFound):
            service.enable_connection(2, connection_id)
        with pytest.raises(McpConnectionNotFound):
            service.disable_connection(2, connection_id)
        assert service.delete_connection(2, connection_id) is DeleteOutcome.NOT_FOUND

    def test_delete_returns_the_durable_outcome_rather_than_raising(self) -> None:
        store = FakeStore()
        service = _service(store)
        connection_id = store.create(
            owner_user_id=1,
            display_name="A",
            transport=ConnectionTransport.HTTP,
            endpoint=ENDPOINT,
            server_key=None,
            credential_ref=None,
            now=NOW,
        )
        store.delete_outcome = DeleteOutcome.REFUSED_HAS_HISTORY
        assert service.delete_connection(1, connection_id) is DeleteOutcome.REFUSED_HAS_HISTORY


class TestMutation:
    def test_rename_changes_only_the_display_name(self) -> None:
        store = FakeStore()
        service = _service(store)
        connection_id = store.create(
            owner_user_id=1,
            display_name="A",
            transport=ConnectionTransport.HTTP,
            endpoint=ENDPOINT,
            server_key=None,
            credential_ref=None,
            now=NOW,
        )
        renamed = service.update_display_name(1, connection_id, " Renamed ")
        assert renamed.display_name == "Renamed"
        assert renamed.transport is ConnectionTransport.HTTP
        assert renamed.endpoint == ENDPOINT

    def test_disable_then_enable_returns_to_needs_refresh(self) -> None:
        store = FakeStore()
        service = _service(store)
        connection_id = store.create(
            owner_user_id=1,
            display_name="A",
            transport=ConnectionTransport.HTTP,
            endpoint=ENDPOINT,
            server_key=None,
            credential_ref=None,
            now=NOW,
        )
        disabled = service.disable_connection(1, connection_id)
        assert disabled.enabled is False
        assert disabled.catalog_status is ConnectionStatus.DISABLED
        enabled = service.enable_connection(1, connection_id)
        assert enabled.enabled is True
        assert enabled.catalog_status is ConnectionStatus.NEEDS_REFRESH


class TestRefresh:
    @pytest.mark.anyio
    async def test_a_successful_refresh_records_materials_and_marks_connected(self) -> None:
        store = FakeStore()
        discovery = FakeDiscovery(result=(_material(1),))
        service = _service(store, discovery=discovery)
        connection_id = store.create(
            owner_user_id=1,
            display_name="A",
            transport=ConnectionTransport.HTTP,
            endpoint=ENDPOINT,
            server_key=None,
            credential_ref=None,
            now=NOW,
        )
        refreshed = await service.refresh(1, connection_id)
        assert discovery.connections[0].connection_id == connection_id
        assert len(store.successes) == 1
        assert store.successes[0]["status"] is ConnectionStatus.CONNECTED
        assert store.successes[0]["materials"] == (_material(1),)
        assert refreshed.catalog_status is ConnectionStatus.CONNECTED
        assert refreshed.last_discovery_at == NOW
        assert refreshed.last_error_code is None

    @pytest.mark.anyio
    async def test_a_classified_failure_is_recorded_as_unavailable_with_its_safe_pair(self) -> None:
        store = FakeStore()
        service = _service(store, discovery=FakeDiscovery(error=ClassifiedError()))
        connection_id = store.create(
            owner_user_id=1,
            display_name="A",
            transport=ConnectionTransport.HTTP,
            endpoint=ENDPOINT,
            server_key=None,
            credential_ref=None,
            now=NOW,
        )
        refreshed = await service.refresh(1, connection_id)
        assert store.failures == [
            {
                "connection_id": connection_id,
                "error_code": "mcp_server_unavailable",
                "error_message": "The MCP server could not be reached.",
            }
        ]
        assert refreshed.catalog_status is ConnectionStatus.UNAVAILABLE
        assert refreshed.last_error_code == "mcp_server_unavailable"

    @pytest.mark.anyio
    async def test_an_unclassified_failure_never_persists_the_exception_text(self) -> None:
        store = FakeStore()
        secret_text = "http://169.254.169.254/token?value=super-secret"
        service = _service(store, discovery=FakeDiscovery(error=RuntimeError(secret_text)))
        connection_id = store.create(
            owner_user_id=1,
            display_name="A",
            transport=ConnectionTransport.HTTP,
            endpoint=ENDPOINT,
            server_key=None,
            credential_ref=None,
            now=NOW,
        )
        refreshed = await service.refresh(1, connection_id)
        assert refreshed.last_error_code == "mcp_discovery_failed"
        assert refreshed.last_error_message == "MCP discovery failed."
        assert secret_text not in (refreshed.last_error_message or "")
        assert all(secret_text not in str(record) for record in store.failures)

    @pytest.mark.anyio
    async def test_a_disabled_connection_is_never_refreshed(self) -> None:
        store = FakeStore()
        discovery = FakeDiscovery(result=(_material(1),))
        service = _service(store, discovery=discovery)
        connection_id = store.create(
            owner_user_id=1,
            display_name="A",
            transport=ConnectionTransport.HTTP,
            endpoint=ENDPOINT,
            server_key=None,
            credential_ref=None,
            now=NOW,
        )
        service.disable_connection(1, connection_id)
        with pytest.raises(InvalidMcpConnection):
            await service.refresh(1, connection_id)
        assert discovery.connections == []

    @pytest.mark.anyio
    async def test_a_connection_removed_during_discovery_is_not_found(self) -> None:
        store = FakeStore()
        connection_id = store.create(
            owner_user_id=1,
            display_name="A",
            transport=ConnectionTransport.HTTP,
            endpoint=ENDPOINT,
            server_key=None,
            credential_ref=None,
            now=NOW,
        )

        class RemovingDiscovery(FakeDiscovery):
            async def __call__(
                self, connection: McpConnectionRow
            ) -> tuple[ToolDefinitionMaterial, ...]:
                del store.rows[connection.connection_id]
                return ()

        service = _service(store, discovery=RemovingDiscovery())
        with pytest.raises(McpConnectionNotFound):
            await service.refresh(1, connection_id)
