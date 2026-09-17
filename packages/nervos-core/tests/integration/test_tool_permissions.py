"""D2 permission tests: grant CRUD and call-time evaluation.

Tests prove the frozen permission precedence and fail-closed defaults. Nothing here executes a
tool, discovers a catalog, or speaks MCP: D2 is pure authorization over the accepted D1 schema, so
the subject of every test is *which grants the durable model permits* and *what the call-time
decision does with them*.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from execution_support import migrate
from nervos_core.application.tool_permissions import (
    GrantAlreadyExists,
    McpConnectionNotFound,
    OwnershipMismatch,
    PermissionDenialReason,
    ToolDefinitionNotFound,
)
from nervos_core.infrastructure.database.models import (
    AgentInstanceRecord,
    AgentToolGrantRecord,
    McpConnectionRecord,
    RunRecord,
    ToolDefinitionRecord,
    ToolInvocationRecord,
    UserRecord,
)
from nervos_core.infrastructure.database.tools import (
    SqlAlchemyToolPermissionEvaluator,
    SqlAlchemyToolPermissionPersistence,
)
from sqlalchemy import Engine, func, insert, select, update

NOW = datetime(2026, 1, 1, tzinfo=UTC)
LATER = datetime(2026, 1, 2, tzinfo=UTC)
BUILTIN_FINGERPRINT = "a" * 64
MCP_FINGERPRINT = "b" * 64
DRIFTED_FINGERPRINT = "c" * 64


def _insert_definition(
    engine: Engine,
    *,
    source_kind: str,
    source_id: int | None,
    upstream_name: str,
    model_name: str,
    fingerprint: str,
    display_name: str = "Tool",
    description: str = "A tool",
) -> int:
    """Insert one Tool Definition and return its durable id."""
    with engine.begin() as conn:
        conn.execute(
            insert(ToolDefinitionRecord).values(
                source_kind=source_kind,
                source_id=source_id,
                upstream_name=upstream_name,
                model_name=model_name,
                display_name=display_name,
                description=description,
                input_schema='{"type":"object","properties":{}}',
                output_schema=None,
                fingerprint=fingerprint,
                status="available",
                created_at=NOW,
                updated_at=NOW,
            )
        )
    with engine.connect() as conn:
        value = conn.execute(
            select(ToolDefinitionRecord.id).where(ToolDefinitionRecord.model_name == model_name)
        ).scalar_one()
    return int(value)


def _insert_connection(
    engine: Engine, *, owner_user_id: int, display_name: str = "TestConn"
) -> int:
    """Insert one MCP connection and return its durable id."""
    with engine.begin() as conn:
        conn.execute(
            insert(McpConnectionRecord).values(
                owner_user_id=owner_user_id,
                display_name=display_name,
                transport="http",
                endpoint="http://test.local",
                server_key=None,
                credential_ref=None,
                enabled=True,
                catalog_status="connected",
                last_discovery_at=NOW,
                last_error_code=None,
                last_error_message=None,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    with engine.connect() as conn:
        value = conn.execute(
            select(McpConnectionRecord.id).where(McpConnectionRecord.display_name == display_name)
        ).scalar_one()
    return int(value)


def _set_connection_enabled(engine: Engine, connection_id: int, enabled: bool) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(McpConnectionRecord)
            .where(McpConnectionRecord.id == connection_id)
            .values(enabled=enabled)
        )


def _set_definition_fingerprint(engine: Engine, definition_id: int, fingerprint: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(ToolDefinitionRecord)
            .where(ToolDefinitionRecord.id == definition_id)
            .values(fingerprint=fingerprint)
        )


def _set_definition_status(engine: Engine, definition_id: int, status: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(ToolDefinitionRecord)
            .where(ToolDefinitionRecord.id == definition_id)
            .values(status=status)
        )


def _insert_run(
    engine: Engine,
    *,
    agent_instance_id: int,
    tool_grant_cutoff_id: int,
    input_text: str = "test",
) -> int:
    """Insert one Run and return its durable id."""
    with engine.begin() as conn:
        conn.execute(
            insert(RunRecord).values(
                agent_instance_id=agent_instance_id,
                status="created",
                agent_key="nervos.chat",
                agent_definition_version="1",
                model_provider="anthropic",
                model_name="claude-test",
                input_text=input_text,
                input_max_bytes=1000000,
                input_max_code_points=500000,
                output_max_bytes=1000000,
                output_max_code_points=500000,
                provider_timeout_ms=300000,
                max_output_tokens=4096,
                max_model_calls=1,
                max_tool_calls=0,
                tool_timeout_ms=30000,
                tool_result_max_bytes=65536,
                max_consecutive_tool_failures=3,
                tool_grant_cutoff_id=tool_grant_cutoff_id,
                created_at=NOW,
            )
        )
    with engine.connect() as conn:
        value = conn.execute(
            select(RunRecord.id).where(RunRecord.input_text == input_text)
        ).scalar_one()
    return int(value)


@pytest.fixture
def temp_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    """A disposable migrated database holding owner (user 1) and one Agent Instance (id 1)."""
    return migrate(tmp_path / "permissions.db", monkeypatch, agents=1)


@pytest.fixture
def users(temp_engine: Engine) -> dict[str, int]:
    """The migrate-created owner (id 1) plus a second user who owns nothing."""
    with temp_engine.begin() as conn:
        conn.execute(
            insert(UserRecord).values(
                username="other",
                password_hash=b"\x00",
                role="admin",
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    with temp_engine.connect() as conn:
        other_id = conn.execute(
            select(UserRecord.id).where(UserRecord.username == "other")
        ).scalar_one()
    return {"owner": 1, "other": int(other_id)}


@pytest.fixture
def agent_instance(temp_engine: Engine) -> int:
    """The Agent Instance created by migrate (id 1)."""
    return 1


@pytest.fixture
def builtin_tool(temp_engine: Engine) -> int:
    """A builtin tool definition."""
    return _insert_definition(
        temp_engine,
        source_kind="builtin",
        source_id=None,
        upstream_name="test_builtin",
        model_name="nervos__builtin__test",
        fingerprint=BUILTIN_FINGERPRINT,
    )


@pytest.fixture
def mcp_connection(temp_engine: Engine) -> int:
    """An enabled MCP connection owned by user 1."""
    return _insert_connection(temp_engine, owner_user_id=1)


@pytest.fixture
def mcp_tool(temp_engine: Engine, mcp_connection: int) -> int:
    """An MCP tool definition from the owner's connection."""
    return _insert_definition(
        temp_engine,
        source_kind="mcp",
        source_id=mcp_connection,
        upstream_name="test_mcp",
        model_name="nervos__c1__test_mcp",
        fingerprint=MCP_FINGERPRINT,
    )


class TestGrantCRUD:
    """Grant creation, idempotency, ownership, and revocation."""

    def test_grant_builtin_tool(
        self, temp_engine: Engine, agent_instance: int, builtin_tool: int
    ) -> None:
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        grant = service.grant_tool(
            owner_user_id=1,
            agent_instance_id=agent_instance,
            tool_definition_id=builtin_tool,
            now=NOW,
        )
        assert grant.agent_instance_id == agent_instance
        assert grant.tool_definition_id == builtin_tool
        assert grant.reviewed_fingerprint == BUILTIN_FINGERPRINT
        assert grant.id > 0

    def test_grant_mcp_tool(self, temp_engine: Engine, agent_instance: int, mcp_tool: int) -> None:
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        grant = service.grant_tool(
            owner_user_id=1,
            agent_instance_id=agent_instance,
            tool_definition_id=mcp_tool,
            now=NOW,
        )
        assert grant.agent_instance_id == agent_instance
        assert grant.tool_definition_id == mcp_tool
        assert grant.reviewed_fingerprint == MCP_FINGERPRINT

    def test_idempotent_when_fingerprint_matches(
        self, temp_engine: Engine, agent_instance: int, builtin_tool: int
    ) -> None:
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        g1 = service.grant_tool(
            owner_user_id=1,
            agent_instance_id=agent_instance,
            tool_definition_id=builtin_tool,
            now=NOW,
        )
        g2 = service.grant_tool(
            owner_user_id=1,
            agent_instance_id=agent_instance,
            tool_definition_id=builtin_tool,
            now=LATER,
        )
        assert g1.id == g2.id
        assert g2.created_at == NOW

    def test_refuses_drifted_definition(
        self, temp_engine: Engine, agent_instance: int, builtin_tool: int
    ) -> None:
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        service.grant_tool(
            owner_user_id=1,
            agent_instance_id=agent_instance,
            tool_definition_id=builtin_tool,
            now=NOW,
        )
        _set_definition_fingerprint(temp_engine, builtin_tool, DRIFTED_FINGERPRINT)
        with pytest.raises(GrantAlreadyExists, match="different fingerprint"):
            service.grant_tool(
                owner_user_id=1,
                agent_instance_id=agent_instance,
                tool_definition_id=builtin_tool,
                now=LATER,
            )

    def test_refuses_foreign_agent(self, temp_engine: Engine, builtin_tool: int) -> None:
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        with pytest.raises(ToolDefinitionNotFound, match="not found or access denied"):
            service.grant_tool(
                owner_user_id=1, agent_instance_id=999, tool_definition_id=builtin_tool, now=NOW
            )

    def test_refuses_foreign_mcp_connection(
        self, temp_engine: Engine, users: dict[str, int], mcp_tool: int
    ) -> None:
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        with temp_engine.begin() as conn:
            conn.execute(
                insert(AgentInstanceRecord).values(
                    owner_user_id=users["other"],
                    agent_key="nervos.chat",
                    agent_definition_version="1",
                    display_name="Other Agent",
                    enabled=True,
                    model_provider="anthropic",
                    model_name="claude-test",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        with temp_engine.connect() as conn:
            other_agent_id = int(
                conn.execute(
                    select(AgentInstanceRecord.id).where(
                        AgentInstanceRecord.display_name == "Other Agent"
                    )
                ).scalar_one()
            )
        with pytest.raises(OwnershipMismatch, match="different owner"):
            service.grant_tool(
                owner_user_id=users["other"],
                agent_instance_id=other_agent_id,
                tool_definition_id=mcp_tool,
                now=NOW,
            )

    def test_refuses_disabled_mcp_connection(
        self, temp_engine: Engine, agent_instance: int, mcp_connection: int, mcp_tool: int
    ) -> None:
        _set_connection_enabled(temp_engine, mcp_connection, False)
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        with pytest.raises(McpConnectionNotFound, match="disabled"):
            service.grant_tool(
                owner_user_id=1,
                agent_instance_id=agent_instance,
                tool_definition_id=mcp_tool,
                now=NOW,
            )

    def test_revoke(self, temp_engine: Engine, agent_instance: int, builtin_tool: int) -> None:
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        service.grant_tool(
            owner_user_id=1,
            agent_instance_id=agent_instance,
            tool_definition_id=builtin_tool,
            now=NOW,
        )
        service.revoke_tool(
            owner_user_id=1, agent_instance_id=agent_instance, tool_definition_id=builtin_tool
        )
        grants = service.list_grants(owner_user_id=1, agent_instance_id=agent_instance)
        assert len(grants) == 0

    def test_revoke_idempotent(
        self, temp_engine: Engine, agent_instance: int, builtin_tool: int
    ) -> None:
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        service.revoke_tool(
            owner_user_id=1, agent_instance_id=agent_instance, tool_definition_id=builtin_tool
        )
        service.revoke_tool(
            owner_user_id=1, agent_instance_id=agent_instance, tool_definition_id=builtin_tool
        )

    def test_revoke_ignores_foreign_agent(self, temp_engine: Engine, builtin_tool: int) -> None:
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        service.revoke_tool(owner_user_id=1, agent_instance_id=999, tool_definition_id=builtin_tool)

    def test_reconfirm_mints_new_identity(
        self, temp_engine: Engine, agent_instance: int, builtin_tool: int
    ) -> None:
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        g1 = service.grant_tool(
            owner_user_id=1,
            agent_instance_id=agent_instance,
            tool_definition_id=builtin_tool,
            now=NOW,
        )
        _set_definition_fingerprint(temp_engine, builtin_tool, DRIFTED_FINGERPRINT)
        g2 = service.reconfirm_tool(
            owner_user_id=1,
            agent_instance_id=agent_instance,
            tool_definition_id=builtin_tool,
            now=LATER,
        )
        assert g2.id > g1.id
        assert g2.reviewed_fingerprint == DRIFTED_FINGERPRINT
        assert g2.created_at == LATER

    def test_list_grants(
        self, temp_engine: Engine, agent_instance: int, builtin_tool: int, mcp_tool: int
    ) -> None:
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        service.grant_tool(
            owner_user_id=1,
            agent_instance_id=agent_instance,
            tool_definition_id=builtin_tool,
            now=NOW,
        )
        service.grant_tool(
            owner_user_id=1, agent_instance_id=agent_instance, tool_definition_id=mcp_tool, now=NOW
        )
        grants = service.list_grants(owner_user_id=1, agent_instance_id=agent_instance)
        assert len(grants) == 2

    def test_list_grants_empty_for_foreign_agent(self, temp_engine: Engine) -> None:
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        grants = service.list_grants(owner_user_id=1, agent_instance_id=999)
        assert len(grants) == 0


class TestPermissionEvaluator:
    """Call-time evaluator: now derives Agent and cutoff from Run."""

    def test_deny_when_no_grant(
        self, temp_engine: Engine, agent_instance: int, builtin_tool: int
    ) -> None:
        run_id = _insert_run(temp_engine, agent_instance_id=agent_instance, tool_grant_cutoff_id=0)
        evaluator = SqlAlchemyToolPermissionEvaluator(temp_engine)
        decision = evaluator.check_permission(
            run_id=run_id,
            tool_definition_id=builtin_tool,
        )
        assert not decision.allowed
        assert decision.reason == PermissionDenialReason.NOT_GRANTED

    def test_allow_when_all_checks_pass(
        self, temp_engine: Engine, agent_instance: int, builtin_tool: int
    ) -> None:
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        grant = service.grant_tool(
            owner_user_id=1,
            agent_instance_id=agent_instance,
            tool_definition_id=builtin_tool,
            now=NOW,
        )
        run_id = _insert_run(
            temp_engine, agent_instance_id=agent_instance, tool_grant_cutoff_id=grant.id
        )
        evaluator = SqlAlchemyToolPermissionEvaluator(temp_engine)
        decision = evaluator.check_permission(
            run_id=run_id,
            tool_definition_id=builtin_tool,
        )
        assert decision.allowed
        assert decision.reason is None

    def test_deny_when_grant_after_cutoff(
        self, temp_engine: Engine, agent_instance: int, builtin_tool: int
    ) -> None:
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        grant = service.grant_tool(
            owner_user_id=1,
            agent_instance_id=agent_instance,
            tool_definition_id=builtin_tool,
            now=NOW,
        )
        run_id = _insert_run(
            temp_engine, agent_instance_id=agent_instance, tool_grant_cutoff_id=grant.id - 1
        )
        evaluator = SqlAlchemyToolPermissionEvaluator(temp_engine)
        decision = evaluator.check_permission(
            run_id=run_id,
            tool_definition_id=builtin_tool,
        )
        assert not decision.allowed
        assert decision.reason == PermissionDenialReason.GRANT_AFTER_RUN_CUTOFF

    def test_deny_when_fingerprint_changed(
        self, temp_engine: Engine, agent_instance: int, builtin_tool: int
    ) -> None:
        """Adversarial: grant reviewed at F1, definition durably becomes F2 -> DENIED.

        No caller parameter can re-supply F1: the evaluator's API takes only a Run id
        and a definition id, so the comparison is between two durable rows.
        """
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        grant = service.grant_tool(
            owner_user_id=1,
            agent_instance_id=agent_instance,
            tool_definition_id=builtin_tool,
            now=NOW,
        )
        assert grant.reviewed_fingerprint == BUILTIN_FINGERPRINT
        run_id = _insert_run(
            temp_engine, agent_instance_id=agent_instance, tool_grant_cutoff_id=grant.id
        )
        evaluator = SqlAlchemyToolPermissionEvaluator(temp_engine)

        # F1: everything agrees, so the call is allowed.
        initial = evaluator.check_permission(
            run_id=run_id,
            tool_definition_id=builtin_tool,
        )
        assert initial.allowed

        # The definition drifts durably to F2 while staying available.
        _set_definition_fingerprint(temp_engine, builtin_tool, DRIFTED_FINGERPRINT)

        # F2: the stored grant is now stale, and nothing the caller can pass revives it.
        decision = evaluator.check_permission(
            run_id=run_id,
            tool_definition_id=builtin_tool,
        )
        assert not decision.allowed
        assert decision.reason == PermissionDenialReason.DEFINITION_CHANGED

    def test_deny_when_definition_is_not_available(
        self, temp_engine: Engine, agent_instance: int, builtin_tool: int
    ) -> None:
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        grant = service.grant_tool(
            owner_user_id=1,
            agent_instance_id=agent_instance,
            tool_definition_id=builtin_tool,
            now=NOW,
        )
        _set_definition_status(temp_engine, builtin_tool, "unavailable")
        run_id = _insert_run(
            temp_engine, agent_instance_id=agent_instance, tool_grant_cutoff_id=grant.id
        )
        evaluator = SqlAlchemyToolPermissionEvaluator(temp_engine)
        decision = evaluator.check_permission(
            run_id=run_id,
            tool_definition_id=builtin_tool,
        )
        assert not decision.allowed
        assert decision.reason == PermissionDenialReason.DEFINITION_UNAVAILABLE

    def test_deny_when_connection_disabled(
        self, temp_engine: Engine, agent_instance: int, mcp_connection: int, mcp_tool: int
    ) -> None:
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        grant = service.grant_tool(
            owner_user_id=1, agent_instance_id=agent_instance, tool_definition_id=mcp_tool, now=NOW
        )
        _set_connection_enabled(temp_engine, mcp_connection, False)
        run_id = _insert_run(
            temp_engine, agent_instance_id=agent_instance, tool_grant_cutoff_id=grant.id
        )
        evaluator = SqlAlchemyToolPermissionEvaluator(temp_engine)
        decision = evaluator.check_permission(
            run_id=run_id,
            tool_definition_id=mcp_tool,
        )
        assert not decision.allowed
        assert decision.reason == PermissionDenialReason.CONNECTION_DISABLED

    def test_revocation_takes_effect_immediately(
        self, temp_engine: Engine, agent_instance: int, builtin_tool: int
    ) -> None:
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        grant = service.grant_tool(
            owner_user_id=1,
            agent_instance_id=agent_instance,
            tool_definition_id=builtin_tool,
            now=NOW,
        )
        run_id = _insert_run(
            temp_engine, agent_instance_id=agent_instance, tool_grant_cutoff_id=grant.id
        )
        evaluator = SqlAlchemyToolPermissionEvaluator(temp_engine)
        decision = evaluator.check_permission(
            run_id=run_id,
            tool_definition_id=builtin_tool,
        )
        assert decision.allowed
        service.revoke_tool(
            owner_user_id=1, agent_instance_id=agent_instance, tool_definition_id=builtin_tool
        )
        decision = evaluator.check_permission(
            run_id=run_id,
            tool_definition_id=builtin_tool,
        )
        assert not decision.allowed
        assert decision.reason == PermissionDenialReason.NOT_GRANTED


class TestMCPPermission:
    """MCP-specific ownership and enabled checks."""

    def test_allow_same_owner_mcp_grant(
        self, temp_engine: Engine, agent_instance: int, mcp_tool: int
    ) -> None:
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        grant = service.grant_tool(
            owner_user_id=1, agent_instance_id=agent_instance, tool_definition_id=mcp_tool, now=NOW
        )
        run_id = _insert_run(
            temp_engine, agent_instance_id=agent_instance, tool_grant_cutoff_id=grant.id
        )
        evaluator = SqlAlchemyToolPermissionEvaluator(temp_engine)
        decision = evaluator.check_permission(
            run_id=run_id,
            tool_definition_id=mcp_tool,
        )
        assert decision.allowed

    def test_re_enabled_connection_restores_eligibility(
        self, temp_engine: Engine, agent_instance: int, mcp_connection: int, mcp_tool: int
    ) -> None:
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        grant = service.grant_tool(
            owner_user_id=1, agent_instance_id=agent_instance, tool_definition_id=mcp_tool, now=NOW
        )
        _set_connection_enabled(temp_engine, mcp_connection, False)
        run_id = _insert_run(
            temp_engine, agent_instance_id=agent_instance, tool_grant_cutoff_id=grant.id
        )
        evaluator = SqlAlchemyToolPermissionEvaluator(temp_engine)
        decision = evaluator.check_permission(
            run_id=run_id,
            tool_definition_id=mcp_tool,
        )
        assert not decision.allowed
        _set_connection_enabled(temp_engine, mcp_connection, True)
        decision = evaluator.check_permission(
            run_id=run_id,
            tool_definition_id=mcp_tool,
        )
        assert decision.allowed

    def test_disabled_connection_keeps_the_grant_row(
        self, temp_engine: Engine, agent_instance: int, mcp_connection: int, mcp_tool: int
    ) -> None:
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        service.grant_tool(
            owner_user_id=1, agent_instance_id=agent_instance, tool_definition_id=mcp_tool, now=NOW
        )
        _set_connection_enabled(temp_engine, mcp_connection, False)
        grants = service.list_grants(owner_user_id=1, agent_instance_id=agent_instance)
        assert len(grants) == 1


class TestRunCutoff:
    """Run cutoff isolation and re-grant behavior."""

    def test_regrant_after_revoke_mints_id_above_old_run(
        self, temp_engine: Engine, agent_instance: int, builtin_tool: int
    ) -> None:
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        g1 = service.grant_tool(
            owner_user_id=1,
            agent_instance_id=agent_instance,
            tool_definition_id=builtin_tool,
            now=NOW,
        )
        run_id = _insert_run(
            temp_engine, agent_instance_id=agent_instance, tool_grant_cutoff_id=g1.id
        )
        service.revoke_tool(
            owner_user_id=1, agent_instance_id=agent_instance, tool_definition_id=builtin_tool
        )
        g2 = service.grant_tool(
            owner_user_id=1,
            agent_instance_id=agent_instance,
            tool_definition_id=builtin_tool,
            now=LATER,
        )
        assert g2.id > g1.id
        evaluator = SqlAlchemyToolPermissionEvaluator(temp_engine)
        decision = evaluator.check_permission(
            run_id=run_id,
            tool_definition_id=builtin_tool,
        )
        assert not decision.allowed
        assert decision.reason == PermissionDenialReason.GRANT_AFTER_RUN_CUTOFF

    def test_reconfirm_does_not_empower_old_run(
        self, temp_engine: Engine, agent_instance: int, builtin_tool: int
    ) -> None:
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        g1 = service.grant_tool(
            owner_user_id=1,
            agent_instance_id=agent_instance,
            tool_definition_id=builtin_tool,
            now=NOW,
        )
        run_id = _insert_run(
            temp_engine, agent_instance_id=agent_instance, tool_grant_cutoff_id=g1.id
        )
        _set_definition_fingerprint(temp_engine, builtin_tool, DRIFTED_FINGERPRINT)
        g2 = service.reconfirm_tool(
            owner_user_id=1,
            agent_instance_id=agent_instance,
            tool_definition_id=builtin_tool,
            now=LATER,
        )
        assert g2.id > g1.id
        evaluator = SqlAlchemyToolPermissionEvaluator(temp_engine)
        decision = evaluator.check_permission(
            run_id=run_id,
            tool_definition_id=builtin_tool,
        )
        assert not decision.allowed
        assert decision.reason == PermissionDenialReason.GRANT_AFTER_RUN_CUTOFF

    def test_grants_at_or_before_cutoff_respects_cutoff(
        self, temp_engine: Engine, agent_instance: int, builtin_tool: int
    ) -> None:
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        g1 = service.grant_tool(
            owner_user_id=1,
            agent_instance_id=agent_instance,
            tool_definition_id=builtin_tool,
            now=NOW,
        )
        selected = service.list_grants_at_or_before_cutoff(
            agent_instance_id=agent_instance, grant_cutoff_id=g1.id
        )
        assert len(selected) == 1
        selected = service.list_grants_at_or_before_cutoff(
            agent_instance_id=agent_instance, grant_cutoff_id=g1.id - 1
        )
        assert len(selected) == 0

    def test_revocation_removes_grant_from_cutoff_window(
        self, temp_engine: Engine, agent_instance: int, builtin_tool: int
    ) -> None:
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        grant = service.grant_tool(
            owner_user_id=1,
            agent_instance_id=agent_instance,
            tool_definition_id=builtin_tool,
            now=NOW,
        )
        before = service.list_grants_at_or_before_cutoff(
            agent_instance_id=agent_instance, grant_cutoff_id=grant.id
        )
        assert len(before) == 1
        service.revoke_tool(
            owner_user_id=1, agent_instance_id=agent_instance, tool_definition_id=builtin_tool
        )
        after = service.list_grants_at_or_before_cutoff(
            agent_instance_id=agent_instance, grant_cutoff_id=grant.id
        )
        assert len(after) == 0


class TestDefaultDenyAndIsolation:
    """Builtin requires grant, annotations do not grant, cross-Agent isolation, read-only."""

    def test_builtin_requires_an_explicit_grant(
        self, temp_engine: Engine, agent_instance: int, builtin_tool: int
    ) -> None:
        run_id = _insert_run(
            temp_engine, agent_instance_id=agent_instance, tool_grant_cutoff_id=999
        )
        evaluator = SqlAlchemyToolPermissionEvaluator(temp_engine)
        decision = evaluator.check_permission(
            run_id=run_id,
            tool_definition_id=builtin_tool,
        )
        assert not decision.allowed

    def test_annotations_do_not_grant_authority(
        self, temp_engine: Engine, agent_instance: int
    ) -> None:
        hint_read_only = _insert_definition(
            temp_engine,
            source_kind="builtin",
            source_id=None,
            upstream_name="annotated",
            model_name="nervos__builtin__annotated",
            fingerprint=BUILTIN_FINGERPRINT,
            display_name="Read Only",
        )
        with temp_engine.begin() as conn:
            conn.execute(
                update(ToolDefinitionRecord)
                .where(ToolDefinitionRecord.id == hint_read_only)
                .values(hint_read_only=True, hint_destructive=False, hint_idempotent=True)
            )
        run_id = _insert_run(
            temp_engine, agent_instance_id=agent_instance, tool_grant_cutoff_id=999
        )
        evaluator = SqlAlchemyToolPermissionEvaluator(temp_engine)
        decision = evaluator.check_permission(
            run_id=run_id,
            tool_definition_id=hint_read_only,
        )
        assert not decision.allowed

    def test_cross_agent_grants_are_isolated(
        self, temp_engine: Engine, agent_instance: int, builtin_tool: int
    ) -> None:
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        grant = service.grant_tool(
            owner_user_id=1,
            agent_instance_id=agent_instance,
            tool_definition_id=builtin_tool,
            now=NOW,
        )
        with temp_engine.begin() as conn:
            conn.execute(
                insert(AgentInstanceRecord).values(
                    owner_user_id=1,
                    agent_key="nervos.chat",
                    agent_definition_version="1",
                    display_name="Agent B",
                    enabled=True,
                    model_provider="anthropic",
                    model_name="claude-test",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        with temp_engine.connect() as conn:
            agent_b_id = int(
                conn.execute(
                    select(AgentInstanceRecord.id).where(
                        AgentInstanceRecord.display_name == "Agent B"
                    )
                ).scalar_one()
            )
        run_b = _insert_run(
            temp_engine,
            agent_instance_id=agent_b_id,
            tool_grant_cutoff_id=grant.id,
            input_text="run_b",
        )
        evaluator = SqlAlchemyToolPermissionEvaluator(temp_engine)
        decision = evaluator.check_permission(
            run_id=run_b,
            tool_definition_id=builtin_tool,
        )
        assert not decision.allowed
        assert decision.reason == PermissionDenialReason.NOT_GRANTED

    def test_evaluator_does_not_mutate_the_database(
        self, temp_engine: Engine, agent_instance: int, builtin_tool: int
    ) -> None:
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        grant = service.grant_tool(
            owner_user_id=1,
            agent_instance_id=agent_instance,
            tool_definition_id=builtin_tool,
            now=NOW,
        )
        run_id = _insert_run(
            temp_engine, agent_instance_id=agent_instance, tool_grant_cutoff_id=grant.id
        )
        with temp_engine.connect() as conn:
            before = (
                int(
                    conn.execute(
                        select(func.count()).select_from(AgentToolGrantRecord)
                    ).scalar_one()
                ),
                int(
                    conn.execute(
                        select(func.count()).select_from(ToolDefinitionRecord)
                    ).scalar_one()
                ),
                int(
                    conn.execute(select(func.count()).select_from(McpConnectionRecord)).scalar_one()
                ),
                int(
                    conn.execute(
                        select(func.count()).select_from(ToolInvocationRecord)
                    ).scalar_one()
                ),
            )
        evaluator = SqlAlchemyToolPermissionEvaluator(temp_engine)
        evaluator.check_permission(
            run_id=run_id,
            tool_definition_id=builtin_tool,
        )
        with temp_engine.connect() as conn:
            after = (
                int(
                    conn.execute(
                        select(func.count()).select_from(AgentToolGrantRecord)
                    ).scalar_one()
                ),
                int(
                    conn.execute(
                        select(func.count()).select_from(ToolDefinitionRecord)
                    ).scalar_one()
                ),
                int(
                    conn.execute(select(func.count()).select_from(McpConnectionRecord)).scalar_one()
                ),
                int(
                    conn.execute(
                        select(func.count()).select_from(ToolInvocationRecord)
                    ).scalar_one()
                ),
            )
        assert before == after
        assert before[3] == 0


class TestCrossAgentAndCutoffAuthority:
    """D2 remediation: evaluator derives Agent and cutoff from Run, caller cannot override."""

    def test_direct_grant_bypassing_service_fails_on_cross_owner_mcp(
        self, temp_engine: Engine
    ) -> None:
        """Malicious direct-insert grant linking User A Agent to User B MCP tool is denied."""
        # User A: id 1 (from migrate)
        # User B: id 2
        with temp_engine.begin() as conn:
            conn.execute(
                insert(UserRecord).values(
                    username="userb",
                    password_hash=b"\x00",
                    role="admin",
                    is_active=True,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        with temp_engine.connect() as conn:
            user_b_id = int(
                conn.execute(
                    select(UserRecord.id).where(UserRecord.username == "userb")
                ).scalar_one()
            )

        # Agent A owned by User A (id 1, from migrate)
        agent_a_id = 1

        # User B's MCP connection
        connection_b_id = _insert_connection(
            temp_engine, owner_user_id=user_b_id, display_name="UserB_Conn"
        )

        # User B's MCP Tool Definition
        tool_b_id = _insert_definition(
            temp_engine,
            source_kind="mcp",
            source_id=connection_b_id,
            upstream_name="user_b_tool",
            model_name="nervos__c999__user_b_tool",
            fingerprint=MCP_FINGERPRINT,
        )

        # MALICIOUS: Directly insert grant bypassing grant_tool service
        with temp_engine.begin() as conn:
            conn.execute(
                insert(AgentToolGrantRecord).values(
                    agent_instance_id=agent_a_id,
                    tool_definition_id=tool_b_id,
                    reviewed_fingerprint=MCP_FINGERPRINT,
                    created_at=NOW,
                )
            )

        # Read back grant id to set a permissive cutoff
        with temp_engine.connect() as conn:
            grant_id = int(
                conn.execute(
                    select(AgentToolGrantRecord.id).where(
                        AgentToolGrantRecord.agent_instance_id == agent_a_id,
                        AgentToolGrantRecord.tool_definition_id == tool_b_id,
                    )
                ).scalar_one()
            )

        # Run A with cutoff >= malicious grant
        run_id = _insert_run(
            temp_engine,
            agent_instance_id=agent_a_id,
            tool_grant_cutoff_id=grant_id,
            input_text="malicious_test",
        )

        # Call-time evaluator MUST deny: Agent A owner != Tool B owner
        evaluator = SqlAlchemyToolPermissionEvaluator(temp_engine)
        decision = evaluator.check_permission(
            run_id=run_id,
            tool_definition_id=tool_b_id,
        )

        assert not decision.allowed
        assert decision.reason == PermissionDenialReason.OWNER_MISMATCH

    def test_run_for_agent_a_cannot_use_agent_b_grant(
        self, temp_engine: Engine, builtin_tool: int
    ) -> None:
        """Cross-Agent isolation: Run's durable Agent determines grant eligibility."""
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        # Agent A (id 1)
        agent_a_id = 1
        # Agent B (same owner)
        with temp_engine.begin() as conn:
            conn.execute(
                insert(AgentInstanceRecord).values(
                    owner_user_id=1,
                    agent_key="nervos.chat",
                    agent_definition_version="1",
                    display_name="Agent B",
                    enabled=True,
                    model_provider="anthropic",
                    model_name="claude-test",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        with temp_engine.connect() as conn:
            agent_b_id = int(
                conn.execute(
                    select(AgentInstanceRecord.id).where(
                        AgentInstanceRecord.display_name == "Agent B"
                    )
                ).scalar_one()
            )
        # Grant to Agent B only
        grant_b = service.grant_tool(
            owner_user_id=1, agent_instance_id=agent_b_id, tool_definition_id=builtin_tool, now=NOW
        )
        # Run A with cutoff covering B's grant
        run_a = _insert_run(
            temp_engine,
            agent_instance_id=agent_a_id,
            tool_grant_cutoff_id=grant_b.id,
            input_text="run_a",
        )
        evaluator = SqlAlchemyToolPermissionEvaluator(temp_engine)
        decision = evaluator.check_permission(
            run_id=run_a,
            tool_definition_id=builtin_tool,
        )
        # Evaluator derives agent_instance_id from Run A, finds no grant for A
        assert not decision.allowed
        assert decision.reason == PermissionDenialReason.NOT_GRANTED

    def test_old_run_denied_when_grant_added_after_cutoff(
        self, temp_engine: Engine, agent_instance: int, builtin_tool: int
    ) -> None:
        """Cutoff enforcement: new grant id > old Run's durable cutoff → denied."""
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        # Old Run with cutoff 0
        old_run = _insert_run(
            temp_engine, agent_instance_id=agent_instance, tool_grant_cutoff_id=0, input_text="old"
        )
        # Grant created after
        grant = service.grant_tool(
            owner_user_id=1,
            agent_instance_id=agent_instance,
            tool_definition_id=builtin_tool,
            now=NOW,
        )
        assert grant.id > 0
        evaluator = SqlAlchemyToolPermissionEvaluator(temp_engine)
        decision = evaluator.check_permission(
            run_id=old_run,
            tool_definition_id=builtin_tool,
        )
        # Evaluator reads Run's cutoff=0, grant.id > 0 → denied
        assert not decision.allowed
        assert decision.reason == PermissionDenialReason.GRANT_AFTER_RUN_CUTOFF

    def test_evaluator_uses_run_durable_cutoff_not_caller_supplied(
        self, temp_engine: Engine, agent_instance: int, builtin_tool: int
    ) -> None:
        """Caller cannot override cutoff: evaluator reads Run's tool_grant_cutoff_id."""
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        grant = service.grant_tool(
            owner_user_id=1,
            agent_instance_id=agent_instance,
            tool_definition_id=builtin_tool,
            now=NOW,
        )
        # Run with cutoff below grant
        low_cutoff_run = _insert_run(
            temp_engine,
            agent_instance_id=agent_instance,
            tool_grant_cutoff_id=grant.id - 1,
            input_text="low",
        )
        evaluator = SqlAlchemyToolPermissionEvaluator(temp_engine)
        # Caller has no way to pass a different cutoff; evaluator reads from Run row
        decision = evaluator.check_permission(
            run_id=low_cutoff_run,
            tool_definition_id=builtin_tool,
        )
        assert not decision.allowed
        assert decision.reason == PermissionDenialReason.GRANT_AFTER_RUN_CUTOFF

    def test_two_agent_instances_same_owner_cannot_share_grant(
        self, temp_engine: Engine, builtin_tool: int
    ) -> None:
        """Same-owner cross-Agent isolation: each Instance's grants are isolated."""
        service = SqlAlchemyToolPermissionPersistence(temp_engine)
        agent_a = 1
        with temp_engine.begin() as conn:
            conn.execute(
                insert(AgentInstanceRecord).values(
                    owner_user_id=1,
                    agent_key="nervos.chat",
                    agent_definition_version="1",
                    display_name="Same Owner B",
                    enabled=True,
                    model_provider="anthropic",
                    model_name="claude-test",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        with temp_engine.connect() as conn:
            agent_b = int(
                conn.execute(
                    select(AgentInstanceRecord.id).where(
                        AgentInstanceRecord.display_name == "Same Owner B"
                    )
                ).scalar_one()
            )
        # Grant to A
        grant_a = service.grant_tool(
            owner_user_id=1, agent_instance_id=agent_a, tool_definition_id=builtin_tool, now=NOW
        )
        # Run B with cutoff covering A's grant
        run_b = _insert_run(
            temp_engine,
            agent_instance_id=agent_b,
            tool_grant_cutoff_id=grant_a.id,
            input_text="run_b",
        )
        evaluator = SqlAlchemyToolPermissionEvaluator(temp_engine)
        decision = evaluator.check_permission(
            run_id=run_b,
            tool_definition_id=builtin_tool,
        )
        # Evaluator derives agent_instance_id=agent_b from Run, no grant for B
        assert not decision.allowed
        assert decision.reason == PermissionDenialReason.NOT_GRANTED
