"""An owner decision resumes the same live Attempt, once, through real durable mediation."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest
from d6_support import NOW, build_rig, claim, descriptor_named, grant, load_run, revoke, submit
from nervos_core.application.approvals import ApprovalService
from nervos_core.application.mcp_connections import ConnectionTransport
from nervos_core.application.tool_invocation_mediator import ToolInvocationMediator
from nervos_core.application.tool_registry import ToolRegistry, ToolResult
from nervos_core.domain.tools import JsonValue, ToolDescriptor
from nervos_core.infrastructure.database.approvals import (
    SqlAlchemyApprovalGate,
    SqlAlchemyApprovalPersistence,
)
from nervos_core.infrastructure.database.mcp_connections import SqlAlchemyMcpConnectionPersistence
from nervos_core.infrastructure.database.models import McpConnectionRecord, ToolDefinitionRecord
from nervos_core.infrastructure.database.tool_invocations import SqlAlchemyToolInvocationPersistence
from nervos_core.infrastructure.database.tools import SqlAlchemyToolPermissionEvaluator
from sqlalchemy import text, update


@pytest.mark.parametrize("decision", ["approve", "deny", "cancel", "revoke"])
def test_same_attempt_approval_resumption_and_refusals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
) -> None:
    rig = build_rig(tmp_path, monkeypatch)
    original = descriptor_named(rig, "calculate")
    connection_id = SqlAlchemyMcpConnectionPersistence(rig.engine).create(
        owner_user_id=1,
        display_name="Fixture",
        transport=ConnectionTransport.HTTP,
        endpoint="https://fixture.example/mcp",
        server_key=None,
        credential_ref=None,
        now=NOW,
    )
    with rig.engine.begin() as conn:
        conn.execute(
            update(McpConnectionRecord)
            .where(McpConnectionRecord.id == connection_id)
            .values(catalog_status="connected")
        )
        conn.execute(
            update(ToolDefinitionRecord)
            .where(ToolDefinitionRecord.id == original.tool_definition_id)
            .values(source_kind="mcp", source_id=connection_id)
        )
    from nervos_core.domain.tools import ToolSourceKind

    descriptor = replace(original, source_kind=ToolSourceKind.MCP, source_id=connection_id)
    grant(rig, descriptor=descriptor)
    run_id = submit(rig)
    handle = claim(rig, run_id)
    dispatched: list[Mapping[str, JsonValue]] = []

    class Tool:
        async def list_tools(self) -> tuple[ToolDescriptor, ...]:
            return (descriptor,)

        async def execute(
            self, descriptor: ToolDescriptor, arguments: Mapping[str, JsonValue]
        ) -> ToolResult:
            dispatched.append(arguments)
            return ToolResult("42")

    registry = ToolRegistry()
    tool = Tool()
    registry.register(source_ref=descriptor.source_ref, source=tool, executor=tool)
    store = SqlAlchemyApprovalPersistence(rig.engine)
    service = ApprovalService(store, lambda: NOW)
    mediator = ToolInvocationMediator(
        registry=registry,
        authorize=SqlAlchemyToolPermissionEvaluator(rig.engine),
        invocations=SqlAlchemyToolInvocationPersistence(rig.engine),
        approvals=SqlAlchemyApprovalGate(rig.engine),
        clock=lambda: NOW,
    )

    async def exercise() -> None:
        task = asyncio.create_task(
            mediator.invoke(
                descriptor=descriptor,
                arguments={"expression": "6 * 7"},
                run=load_run(rig, run_id),
                claim=handle,
                tool_sequence=1,
                provider_call_id="fixture-call",
            )
        )
        for _ in range(50):
            rows = service.list_pending(owner_user_id=1)
            if rows:
                break
            await asyncio.sleep(0.01)
        else:
            task.cancel()
            pytest.fail("live invocation never requested approval")
        approval = rows[0]
        assert approval.attempt_id == handle.attempt_id
        if decision == "revoke":
            revoke(rig, descriptor=descriptor)
        elif decision == "cancel":
            service.cancel(owner_user_id=1, approval_id=approval.id)
        else:
            service.decide(owner_user_id=1, approval_id=approval.id, approve=decision == "approve")
        result = await asyncio.wait_for(task, 3)
        assert result.denied is (decision != "approve")

    asyncio.run(exercise())
    assert len(dispatched) == (1 if decision == "approve" else 0)
    with rig.engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM tool_invocations")) == 1
        assert conn.scalar(text("SELECT count(*) FROM job_attempts")) == 1
        state = conn.scalar(text("SELECT state FROM action_approvals"))
        assert (
            state
            == {
                "approve": "consumed",
                "deny": "denied",
                "cancel": "cancelled",
                "revoke": "cancelled",
            }[decision]
        )
        assert (
            conn.scalar(
                text("SELECT count(*) FROM run_events WHERE event_type='tool.approval_requested'")
            )
            == 1
        )
        assert (
            conn.scalar(
                text("SELECT count(*) FROM run_events WHERE event_type='tool.approval_decided'")
            )
            == 1
        )
