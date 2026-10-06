"""Transactional persistence for reviewed runtime integrations (ADR 0031)."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import Connection, Engine, delete, func, insert, or_, select, update
from sqlalchemy.engine import RowMapping

from nervos_core.application.context_builder import ContextBuilder
from nervos_core.application.package_manifest import parse_package_manifest
from nervos_core.application.runtime_integration import (
    IntegrationConflict,
    IntegrationNotFound,
    MemoryProposal,
    PackageIntegration,
    package_integration,
    parse_memory_proposals,
)
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.context import (
    ContextSnapshotData,
    deserialize_selected_memories,
    serialize_selected_memories,
)
from nervos_core.domain.runs import RunLimits
from nervos_core.infrastructure.database.memory import _retrieve_memory_candidates_on_connection
from nervos_core.infrastructure.database.models import (
    AgentInstancePackageBindingRecord,
    AgentInstanceRecord,
    AgentMemoryPolicyRecord,
    AgentToolBindingRecord,
    AgentToolGrantRecord,
    ConversationRecord,
    ConversationRunLinkRecord,
    ConversationTurnRecord,
    InstalledPackageVersionRecord,
    JobRecord,
    McpConnectionRecord,
    MemoryItemRecord,
    MemorySuggestionRecord,
    MemoryVersionRecord,
    RunIntegrationRecord,
    RunRecord,
    ToolDefinitionRecord,
    ToolInvocationRecord,
    WorkerRecord,
)
from nervos_core.infrastructure.database.tools import SqlAlchemyToolPermissionPersistence
from nervos_core.infrastructure.database.transaction import TransactionRunner

# Static operator copy for the package-sandbox projection. The projection never
# carries an exception, a path, or a worker identity -- only these bounded sentences.
_SANDBOX_NO_WORKER_REASON = (
    "No execution Worker is currently available to report package sandbox support."
)
_SANDBOX_PREREQUISITE_REASON = "Package isolation requires bubblewrap (bwrap) on the Worker host."
_SANDBOX_UNSUPPORTED_PLATFORM_REASON = (
    "Package execution is qualified only on Linux with bubblewrap."
)


def _package_sandbox_projection(workers: Sequence[Any]) -> dict[str, Any]:
    """Aggregate live Worker observations into one bounded, credential-free projection.

    A single supporting Worker licenses the positive statement; otherwise the projection
    stays negative and names either the missing prerequisite or the unsupported platform.
    Nothing here is execution authority: the launch factory re-decides every package start.
    """
    reporting = list(workers)
    if not reporting:
        return {
            "supported": False,
            "platform": "unknown",
            "backend": None,
            "reason": _SANDBOX_NO_WORKER_REASON,
        }
    qualified = next((row for row in reporting if row.package_execution_supported), None)
    if qualified is not None:
        return {
            "supported": True,
            "platform": str(qualified.platform or "unknown"),
            "backend": str(qualified.sandbox_backend or "unknown"),
            "reason": None,
        }
    platforms = sorted({str(row.platform) for row in reporting if row.platform})
    platform = platforms[0] if len(platforms) == 1 else "mixed"
    reason = (
        _SANDBOX_PREREQUISITE_REASON
        if platforms == ["linux"]
        else _SANDBOX_UNSUPPORTED_PLATFORM_REASON
    )
    return {"supported": False, "platform": platform, "backend": None, "reason": reason}


def eligible_conversation_source(conn: Connection, run_id: int) -> bool:
    """Deleted or superseded conversational results cannot create facts or paid extraction work."""
    row = conn.execute(
        select(ConversationRecord.status, ConversationTurnRecord.authoritative_run_id)
        .select_from(ConversationRunLinkRecord)
        .join(
            ConversationTurnRecord, ConversationTurnRecord.id == ConversationRunLinkRecord.turn_id
        )
        .join(ConversationRecord, ConversationRecord.id == ConversationTurnRecord.conversation_id)
        .where(ConversationRunLinkRecord.run_id == run_id)
    ).one_or_none()
    return row is None or (row.status != "deleted" and row.authoritative_run_id == run_id)


def _instance(conn: Connection, owner: int, instance: int) -> RowMapping:
    row = (
        conn.execute(
            select(AgentInstanceRecord).where(
                AgentInstanceRecord.id == instance, AgentInstanceRecord.owner_user_id == owner
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise IntegrationNotFound
    return row


def _definition(conn: Connection, owner: int, definition: int) -> RowMapping:
    row = (
        conn.execute(select(ToolDefinitionRecord).where(ToolDefinitionRecord.id == definition))
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise IntegrationNotFound
    if row["source_kind"] == "mcp":
        owned = conn.scalar(
            select(McpConnectionRecord.id).where(
                McpConnectionRecord.id == row["source_id"],
                McpConnectionRecord.owner_user_id == owner,
            )
        )
        if owned is None:
            raise IntegrationNotFound
    return row


def _package(conn: Connection, instance: int) -> PackageIntegration:
    manifest = conn.scalar(
        select(InstalledPackageVersionRecord.manifest_bytes)
        .join(
            AgentInstancePackageBindingRecord,
            AgentInstancePackageBindingRecord.installed_package_version_id
            == InstalledPackageVersionRecord.id,
        )
        .where(AgentInstancePackageBindingRecord.agent_instance_id == instance)
    )
    return (
        package_integration(parse_package_manifest(manifest))
        if manifest is not None
        else PackageIntegration()
    )


def _policy(conn: Connection, instance: int) -> dict[str, Any]:
    row = (
        conn.execute(
            select(AgentMemoryPolicyRecord).where(
                AgentMemoryPolicyRecord.agent_instance_id == instance
            )
        )
        .mappings()
        .one_or_none()
    )
    return {
        "mode": row["mode"] if row else "manual",
        "revision": row["revision"] if row else 0,
        "extraction_enabled": bool(row["extraction_enabled"]) if row else False,
    }


def snapshot_json(snapshot: ContextSnapshotData) -> str:
    return json.dumps(
        {
            "input": snapshot.current_user_text,
            "rendered": snapshot.rendered_context,
            "memories": serialize_selected_memories(snapshot.selected_memories),
            "user_memory": snapshot.injected_user_memory_text,
            "agent_memory": snapshot.injected_agent_memory_text,
            "digest": snapshot.content_digest.hex(),
            "created_at": snapshot.created_at.isoformat(),
            "agent_key": snapshot.agent_key,
            "agent_version": snapshot.agent_definition_version,
            "max_bytes": snapshot.max_total_bytes,
            "max_points": snapshot.max_total_code_points,
        },
        ensure_ascii=False,
    )


def snapshot_from_json(raw: str, run_id: int) -> ContextSnapshotData:
    data: dict[str, Any] = json.loads(raw)
    rendered = str(data["rendered"])
    digest = bytes.fromhex(str(data["digest"]))
    if hashlib.sha256(rendered.encode()).digest() != digest:
        raise ValueError("execution context digest mismatch")
    return ContextSnapshotData(
        run_id=run_id,
        turn_id=0,
        schema_version=2,
        builder_version="nervos.context.v2",
        current_user_text=str(data["input"]),
        history_messages=(),
        selected_turn_ids=(),
        selected_message_ids=(),
        compaction_version=None,
        compaction_source_start=None,
        compaction_source_end=None,
        injected_compaction_text=None,
        agent_key=str(data["agent_key"]),
        agent_definition_version=str(data["agent_version"]),
        max_total_bytes=int(data["max_bytes"]),
        max_total_code_points=int(data["max_points"]),
        actual_total_bytes=len(rendered.encode()),
        actual_total_code_points=len(rendered),
        rendered_context=rendered,
        content_digest=digest,
        created_at=datetime.fromisoformat(str(data["created_at"])),
        selected_memories=deserialize_selected_memories(str(data["memories"])),
        injected_user_memory_text=data["user_memory"],
        injected_agent_memory_text=data["agent_memory"],
    )


def independent_context(
    conn: Connection,
    owner: int,
    instance: int,
    definition: AgentDefinitionId,
    input_text: str,
    limits: RunLimits,
    now: datetime,
) -> ContextSnapshotData:
    return ContextBuilder.assemble(
        current_user_text=input_text,
        candidate_turns=[],
        current_compaction=None,
        agent_definition_id=definition,
        now=now,
        memory_candidates=_retrieve_memory_candidates_on_connection(conn, owner, instance),
        max_bytes=limits.input_max_bytes,
        max_code_points=limits.input_max_code_points,
    )


def capture_integration(
    conn: Connection, run_id: int, instance: RowMapping, context: ContextSnapshotData | None
) -> None:
    policy = _policy(conn, int(instance["id"]))
    bindings = (
        conn.execute(
            select(AgentToolBindingRecord).where(
                AgentToolBindingRecord.agent_instance_id == instance["id"],
                AgentToolBindingRecord.package_id == instance["agent_key"],
                AgentToolBindingRecord.package_version == instance["agent_definition_version"],
            )
        )
        .mappings()
        .all()
    )
    conn.execute(
        insert(RunIntegrationRecord).values(
            run_id=run_id,
            context_json=snapshot_json(context) if context else None,
            bindings_json=json.dumps(
                [
                    {
                        "alias": row["alias"],
                        "definition_id": row["tool_definition_id"],
                        "fingerprint": row["reviewed_fingerprint"],
                    }
                    for row in bindings
                ]
            ),
            memory_mode=policy["mode"],
            policy_revision=policy["revision"],
            extraction_enabled=policy["extraction_enabled"],
            proposals_json="[]",
            memory_state="none",
        )
    )


def capture_proposals(conn: Connection, run_id: int, proposals: tuple[MemoryProposal, ...]) -> None:
    parent = conn.scalar(
        select(RunIntegrationRecord.run_id).where(RunIntegrationRecord.extraction_run_id == run_id)
    )
    if parent is not None:
        conn.execute(
            update(RunIntegrationRecord)
            .where(RunIntegrationRecord.run_id == parent)
            .values(
                proposals_json=json.dumps([asdict(p) for p in proposals]),
                memory_state="pending" if proposals else "no_facts",
            )
        )
        return
    row = (
        conn.execute(select(RunIntegrationRecord).where(RunIntegrationRecord.run_id == run_id))
        .mappings()
        .one_or_none()
    )
    if row is None or row["memory_mode"] == "manual":
        return
    conn.execute(
        update(RunIntegrationRecord)
        .where(RunIntegrationRecord.run_id == run_id)
        .values(
            proposals_json=json.dumps([asdict(p) for p in proposals]),
            memory_state="pending"
            if proposals
            else ("extraction_pending" if row["extraction_enabled"] else "none"),
        )
    )


class SqlAlchemyRuntimeIntegrationPersistence:
    def __init__(
        self, engine: Engine, admission_limits: tuple[int, int, int] = (1000, 1000, 1000)
    ) -> None:
        self._engine = engine
        self._runner = TransactionRunner(engine, time.sleep)
        self._permissions = SqlAlchemyToolPermissionPersistence(engine)
        self._admission_limits = admission_limits

    def tools(
        self, owner: int, connection: int | None = None, before_id: int | None = None
    ) -> dict[str, Any]:
        with self._engine.connect() as conn:
            if (
                connection is not None
                and conn.scalar(
                    select(McpConnectionRecord.id).where(
                        McpConnectionRecord.id == connection,
                        McpConnectionRecord.owner_user_id == owner,
                    )
                )
                is None
            ):
                raise IntegrationNotFound
            query = (
                select(ToolDefinitionRecord)
                .outerjoin(
                    McpConnectionRecord, ToolDefinitionRecord.source_id == McpConnectionRecord.id
                )
                .where(
                    or_(
                        ToolDefinitionRecord.source_kind == "builtin",
                        McpConnectionRecord.owner_user_id == owner,
                    )
                )
            )
            if connection is not None:
                query = query.where(
                    ToolDefinitionRecord.source_kind == "mcp",
                    ToolDefinitionRecord.source_id == connection,
                )
            if before_id is not None:
                query = query.where(ToolDefinitionRecord.id < before_id)
            rows = (
                conn.execute(query.order_by(ToolDefinitionRecord.id.desc()).limit(201))
                .mappings()
                .all()
            )
            return {
                "items": [
                    {
                        "id": r["id"],
                        "name": r["upstream_name"],
                        "display_name": r["display_name"],
                        "source_kind": r["source_kind"],
                        "connection_id": r["source_id"],
                        "status": r["status"],
                        "fingerprint": r["fingerprint"],
                        "input_schema_sha256": hashlib.sha256(
                            str(r["input_schema"]).encode()
                        ).hexdigest(),
                    }
                    for r in rows[:200]
                ],
                "next_before_id": rows[199]["id"] if len(rows) > 200 else None,
            }

    def agent_tools(self, owner: int, instance: int) -> dict[str, Any]:
        with self._engine.connect() as conn:
            agent = _instance(conn, owner, instance)
            integration = _package(conn, instance)
            bindings = (
                conn.execute(
                    select(AgentToolBindingRecord).where(
                        AgentToolBindingRecord.agent_instance_id == instance,
                        AgentToolBindingRecord.package_id == agent["agent_key"],
                        AgentToolBindingRecord.package_version == agent["agent_definition_version"],
                    )
                )
                .mappings()
                .all()
            )
            grants = (
                conn.execute(
                    select(AgentToolGrantRecord).where(
                        AgentToolGrantRecord.agent_instance_id == instance
                    )
                )
                .mappings()
                .all()
            )
            audit = (
                conn.execute(
                    select(ToolInvocationRecord)
                    .join(RunRecord, RunRecord.id == ToolInvocationRecord.run_id)
                    .where(RunRecord.agent_instance_id == instance)
                    .order_by(ToolInvocationRecord.id.desc())
                    .limit(50)
                )
                .mappings()
                .all()
            )
            return {
                "requirements": [asdict(t) for t in integration.tools],
                "bindings": [
                    {
                        "alias": r["alias"],
                        "definition_id": r["tool_definition_id"],
                        "fingerprint": r["reviewed_fingerprint"],
                    }
                    for r in bindings
                ],
                "grants": [
                    {
                        "definition_id": r["tool_definition_id"],
                        "fingerprint": r["reviewed_fingerprint"],
                    }
                    for r in grants
                ],
                "history": [
                    {
                        "id": r["id"],
                        "run_id": r["run_id"],
                        "name": r["upstream_name"],
                        "status": r["status"],
                        "error_code": r["error_code"],
                    }
                    for r in audit
                ],
            }

    def bind_tool(
        self, owner: int, instance: int, alias: str, definition: int, now: datetime
    ) -> None:
        def operation(conn: Connection) -> None:
            agent = _instance(conn, owner, instance)
            requirement = next(
                (t for t in _package(conn, instance).tools if t.alias == alias), None
            )
            tool = _definition(conn, owner, definition)
            if (
                requirement is None
                or (
                    tool["source_kind"] != "mcp"
                    and not (
                        tool["source_kind"] == "builtin"
                        and tool["upstream_name"]
                        in ("gmail.users.messages.list", "gmail.users.messages.get")
                    )
                )
                or tool["status"] != "available"
                or tool["upstream_name"] != requirement.upstream_name
                or hashlib.sha256(str(tool["input_schema"]).encode()).hexdigest()
                != requirement.input_schema_sha256
            ):
                raise IntegrationConflict("tool does not match its declared MCP identity/schema")
            conn.execute(
                delete(AgentToolBindingRecord).where(
                    AgentToolBindingRecord.agent_instance_id == instance,
                    AgentToolBindingRecord.alias == alias,
                )
            )
            conn.execute(
                insert(AgentToolBindingRecord).values(
                    agent_instance_id=instance,
                    alias=alias,
                    package_id=agent["agent_key"],
                    package_version=agent["agent_definition_version"],
                    tool_definition_id=definition,
                    reviewed_fingerprint=tool["fingerprint"],
                    updated_at=now,
                )
            )

        self._runner.run(operation)

    def unbind_tool(self, owner: int, instance: int, alias: str) -> None:
        def operation(conn: Connection) -> None:
            _instance(conn, owner, instance)
            conn.execute(
                delete(AgentToolBindingRecord).where(
                    AgentToolBindingRecord.agent_instance_id == instance,
                    AgentToolBindingRecord.alias == alias,
                )
            )

        self._runner.run(operation)

    def grant_tool(
        self, owner: int, instance: int, definition: int, action: str, now: datetime
    ) -> None:
        with self._engine.connect() as conn:
            _instance(conn, owner, instance)
            _definition(conn, owner, definition)
        kwargs = {
            "owner_user_id": owner,
            "agent_instance_id": instance,
            "tool_definition_id": definition,
        }
        if action == "revoke":
            self._permissions.revoke_tool(**kwargs)
        elif action == "reconfirm":
            self._permissions.reconfirm_tool(**kwargs, now=now)
        else:
            self._permissions.grant_tool(**kwargs, now=now)

    def policy(self, owner: int, instance: int) -> dict[str, Any]:
        with self._engine.connect() as conn:
            _instance(conn, owner, instance)
            return _policy(conn, instance)

    def set_policy(
        self, owner: int, instance: int, mode: str, extraction: bool, revision: int, now: datetime
    ) -> dict[str, Any]:
        def operation(conn: Connection) -> dict[str, Any]:
            _instance(conn, owner, instance)
            current = _policy(conn, instance)
            if current["revision"] != revision:
                raise IntegrationConflict("memory policy changed; refresh before saving")
            conn.execute(
                delete(AgentMemoryPolicyRecord).where(
                    AgentMemoryPolicyRecord.agent_instance_id == instance
                )
            )
            conn.execute(
                insert(AgentMemoryPolicyRecord).values(
                    agent_instance_id=instance,
                    mode=mode,
                    revision=revision + 1,
                    extraction_enabled=extraction,
                    updated_at=now,
                )
            )
            return _policy(conn, instance)

        return self._runner.run(operation)

    def suggestions(
        self, owner: int, instance: int | None = None, before_id: int | None = None
    ) -> dict[str, Any]:
        with self._engine.connect() as conn:
            query = select(MemorySuggestionRecord).where(
                MemorySuggestionRecord.owner_user_id == owner
            )
            if instance is not None:
                _instance(conn, owner, instance)
                query = query.where(MemorySuggestionRecord.agent_instance_id == instance)
            if before_id is not None:
                query = query.where(MemorySuggestionRecord.id < before_id)
            rows = (
                conn.execute(query.order_by(MemorySuggestionRecord.id.desc()).limit(51))
                .mappings()
                .all()
            )
            return {
                "items": [
                    {
                        "id": r["id"],
                        "agent_instance_id": r["agent_instance_id"],
                        "source_run_id": r["source_run_id"],
                        "scope": r["scope"],
                        "content": r["content"],
                        "state": r["state"],
                        "memory_item_id": r["memory_item_id"],
                    }
                    for r in rows[:50]
                ],
                "next_before_id": rows[49]["id"] if len(rows) > 50 else None,
            }

    def decide_suggestion(self, owner: int, suggestion: int, approve: bool, now: datetime) -> None:
        def operation(conn: Connection) -> None:
            row = (
                conn.execute(
                    select(MemorySuggestionRecord).where(
                        MemorySuggestionRecord.id == suggestion,
                        MemorySuggestionRecord.owner_user_id == owner,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise IntegrationNotFound
            if row["state"] != "pending":
                raise IntegrationConflict("suggestion already resolved")
            if approve:
                _save_suggestion(conn, row, now)
            else:
                conn.execute(
                    update(MemorySuggestionRecord)
                    .where(MemorySuggestionRecord.id == suggestion)
                    .values(state="dismissed")
                )

        self._runner.run(operation)

    def bindings_for_run(self, run: int) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            raw = conn.scalar(
                select(RunIntegrationRecord.bindings_json).where(RunIntegrationRecord.run_id == run)
            )
            return cast(list[dict[str, Any]], json.loads(raw)) if raw else []

    def is_extraction_run(self, run: int) -> bool:
        with self._engine.connect() as conn:
            return (
                conn.scalar(
                    select(RunIntegrationRecord.run_id).where(
                        RunIntegrationRecord.extraction_run_id == run
                    )
                )
                is not None
            )

    def maintain_memory(self, now: datetime) -> None:
        from nervos_core.application.errors import QueueCapacityExceeded
        from nervos_core.infrastructure.database.conversations import (
            SqlAlchemyConversationPersistence,
        )
        from nervos_core.infrastructure.database.jobs import insert_run_and_job_on_connection

        with self._engine.connect() as conn:
            pending = conn.execute(
                select(RunRecord.id, RunRecord.output_text)
                .join(RunIntegrationRecord, RunIntegrationRecord.run_id == RunRecord.id)
                .where(
                    RunRecord.status == "succeeded",
                    RunIntegrationRecord.memory_state.in_(("pending", "extraction_pending")),
                )
                .limit(50)
            ).all()
        conversations = SqlAlchemyConversationPersistence(self._engine)
        for source, output in pending:
            conversations.project_terminal_run(
                run_id=int(source), status="succeeded", output_text=output, error_code=None, now=now
            )

        def operation(conn: Connection) -> None:
            rows = (
                conn.execute(
                    select(
                        RunIntegrationRecord,
                        RunRecord.agent_instance_id,
                        RunRecord.input_text,
                        RunRecord.output_text,
                        AgentInstanceRecord.owner_user_id,
                    )
                    .join(RunRecord, RunRecord.id == RunIntegrationRecord.run_id)
                    .join(
                        AgentInstanceRecord, AgentInstanceRecord.id == RunRecord.agent_instance_id
                    )
                    .where(
                        RunRecord.status == "succeeded",
                        RunIntegrationRecord.memory_state == "extraction_pending",
                    )
                    .order_by(RunIntegrationRecord.run_id)
                    .limit(10)
                )
                .mappings()
                .all()
            )
            for row in rows:
                source = int(row["run_id"])
                instance = int(row["agent_instance_id"])
                policy = _policy(conn, instance)
                link = (
                    conn.execute(
                        select(ConversationRunLinkRecord).where(
                            ConversationRunLinkRecord.run_id == source
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if (
                    link
                    and conn.scalar(
                        select(ConversationTurnRecord.authoritative_run_id).where(
                            ConversationTurnRecord.id == link["turn_id"]
                        )
                    )
                    != source
                ):
                    continue
                if (
                    policy["revision"] != row["policy_revision"]
                    or not policy["extraction_enabled"]
                    or policy["mode"] == "manual"
                ):
                    conn.execute(
                        update(RunIntegrationRecord)
                        .where(RunIntegrationRecord.run_id == source)
                        .values(memory_state="policy_changed")
                    )
                    continue
                agent = _instance(conn, int(row["owner_user_id"]), instance)
                if not agent["enabled"]:
                    conn.execute(
                        update(RunIntegrationRecord)
                        .where(RunIntegrationRecord.run_id == source)
                        .values(memory_state="agent_disabled")
                    )
                    continue
                excerpt = json.dumps(
                    {
                        "input": str(row["input_text"])[:1000],
                        "response": str(row["output_text"])[:1000],
                    },
                    ensure_ascii=False,
                )
                while len(excerpt.encode()) > 7500 or len(excerpt) > 3900:
                    excerpt = excerpt[: len(excerpt) // 2]
                try:
                    derived = insert_run_and_job_on_connection(
                        conn,
                        owner_user_id=int(row["owner_user_id"]),
                        agent_instance_id=instance,
                        input_text=excerpt,
                        limits=RunLimits(
                            max_output_tokens=1024,
                            provider_timeout_ms=30000,
                            max_model_calls=1,
                            max_tool_calls=0,
                        ),
                        definition_id=AgentDefinitionId(
                            str(agent["agent_key"]), str(agent["agent_definition_version"])
                        ),
                        now=now,
                        max_attempts=1,
                        capacity=self._admission_limits[0],
                        agent_capacity=self._admission_limits[1],
                        provider_capacity=self._admission_limits[2],
                    )
                except QueueCapacityExceeded:
                    continue
                conn.execute(
                    update(RunIntegrationRecord)
                    .where(RunIntegrationRecord.run_id == source)
                    .values(extraction_run_id=derived.id, memory_state="extraction_queued")
                )
                conn.execute(
                    update(RunIntegrationRecord)
                    .where(RunIntegrationRecord.run_id == derived.id)
                    .values(memory_mode="manual", extraction_enabled=False)
                )
            failed = select(RunRecord.id).where(RunRecord.status.in_(("failed", "cancelled")))
            conn.execute(
                update(RunIntegrationRecord)
                .where(
                    RunIntegrationRecord.memory_state == "extraction_queued",
                    RunIntegrationRecord.extraction_run_id.in_(failed),
                )
                .values(memory_state="extraction_failed")
            )

        self._runner.run(operation)
        self.materialize_pending(now)

    def run_context(self, owner: int, run: int) -> dict[str, Any]:
        with self._engine.connect() as conn:
            owned = conn.scalar(
                select(RunRecord.id)
                .join(AgentInstanceRecord, AgentInstanceRecord.id == RunRecord.agent_instance_id)
                .where(RunRecord.id == run, AgentInstanceRecord.owner_user_id == owner)
            )
            if owned is None:
                raise IntegrationNotFound
            record = (
                conn.execute(select(RunIntegrationRecord).where(RunIntegrationRecord.run_id == run))
                .mappings()
                .one_or_none()
            )
            from nervos_core.infrastructure.database.conversations import (
                SqlAlchemyConversationPersistence,
            )

            snapshot = SqlAlchemyConversationPersistence(self._engine).load_run_context_snapshot(
                run
            )
            if snapshot is None and record and record["context_json"]:
                snapshot = snapshot_from_json(str(record["context_json"]), run)
            return {
                "available": snapshot is not None,
                "current_input": snapshot.current_user_text if snapshot else None,
                "rendered_context": snapshot.rendered_context if snapshot else None,
                "memories": [
                    {"id": m.item_id, "version": m.version, "scope": m.scope, "content": m.content}
                    for m in snapshot.selected_memories
                ]
                if snapshot
                else [],
                "history": [
                    {"role": m.role.value, "content": m.content, "turn": m.sequence}
                    for m in snapshot.history_messages
                ]
                if snapshot
                else [],
                "memory_state": record["memory_state"] if record else "none",
                "context_bytes": snapshot.actual_total_bytes if snapshot else 0,
                "context_limit_bytes": snapshot.max_total_bytes if snapshot else 0,
            }

    def health(self, owner: int, now: datetime) -> dict[str, Any]:
        """Report observed availability, Worker-reported sandbox capability, owner workload.

        The capability projection is built exclusively from live Worker rows: the process
        that executes packages is the only one whose observation is worth publishing.
        Nothing here exposes a worker id, hostname, path, environment or error text, and
        nothing here authorizes execution -- the launch factory decides per start.
        """
        with self._engine.connect() as conn:
            workers = conn.execute(
                select(
                    WorkerRecord.platform,
                    WorkerRecord.sandbox_backend,
                    WorkerRecord.package_execution_supported,
                )
                .where(
                    WorkerRecord.stopped_at.is_(None),
                    WorkerRecord.last_heartbeat_at >= now - timedelta(seconds=30),
                )
                .order_by(WorkerRecord.id)
            ).all()
            counts = conn.execute(
                select(JobRecord.status, func.count())
                .join(AgentInstanceRecord, AgentInstanceRecord.id == JobRecord.agent_instance_id)
                .where(AgentInstanceRecord.owner_user_id == owner)
                .group_by(JobRecord.status)
            ).all()
            return {
                "observed_at": now.isoformat(),
                "execution_available": bool(workers),
                "package_sandbox": _package_sandbox_projection(workers),
                "owner_jobs": {str(state): count for state, count in counts},
            }

    def materialize_pending(self, now: datetime) -> int:
        def operation(conn: Connection) -> int:
            rows = (
                conn.execute(
                    select(
                        RunIntegrationRecord,
                        RunRecord.agent_instance_id,
                        AgentInstanceRecord.owner_user_id,
                    )
                    .join(RunRecord, RunRecord.id == RunIntegrationRecord.run_id)
                    .join(
                        AgentInstanceRecord, AgentInstanceRecord.id == RunRecord.agent_instance_id
                    )
                    .where(
                        RunRecord.status == "succeeded",
                        RunIntegrationRecord.memory_state == "pending",
                    )
                    .order_by(RunIntegrationRecord.run_id)
                    .limit(50)
                )
                .mappings()
                .all()
            )
            completed = 0
            for row in rows:
                source = int(row["run_id"])
                link = (
                    conn.execute(
                        select(ConversationRunLinkRecord).where(
                            ConversationRunLinkRecord.run_id == source
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if link and not eligible_conversation_source(conn, source):
                    conn.execute(
                        update(RunIntegrationRecord)
                        .where(RunIntegrationRecord.run_id == source)
                        .values(memory_state="source_ineligible")
                    )
                    continue
                policy = _policy(conn, int(row["agent_instance_id"]))
                if policy["mode"] == "manual" or policy["revision"] != row["policy_revision"]:
                    conn.execute(
                        update(RunIntegrationRecord)
                        .where(RunIntegrationRecord.run_id == source)
                        .values(memory_state="policy_changed")
                    )
                    continue
                proposals = parse_memory_proposals(json.loads(str(row["proposals_json"])))
                for proposal in proposals:
                    digest = hashlib.sha256(proposal.content.encode()).digest()
                    prior = conn.scalar(
                        select(MemorySuggestionRecord.id).where(
                            MemorySuggestionRecord.source_run_id == source,
                            MemorySuggestionRecord.scope == proposal.scope,
                            MemorySuggestionRecord.content_digest == digest,
                        )
                    )
                    if prior is not None:
                        continue
                    item = (
                        conn.execute(
                            insert(MemorySuggestionRecord)
                            .values(
                                owner_user_id=row["owner_user_id"],
                                agent_instance_id=row["agent_instance_id"],
                                source_run_id=source,
                                scope=proposal.scope,
                                content=proposal.content,
                                content_digest=digest,
                                state="pending",
                                created_at=now,
                            )
                            .returning(MemorySuggestionRecord)
                        )
                        .mappings()
                        .one()
                    )
                    if (
                        row["memory_mode"] == "automatic_private"
                        and policy["mode"] == "automatic_private"
                        and proposal.scope == "agent"
                    ):
                        _save_suggestion(conn, item, now, automatic=True)
                conn.execute(
                    update(RunIntegrationRecord)
                    .where(RunIntegrationRecord.run_id == source)
                    .values(memory_state="processed")
                )
                completed += 1
            return completed

        return self._runner.run(operation)


def _save_suggestion(
    conn: Connection, row: RowMapping, now: datetime, *, automatic: bool = False
) -> None:
    agent = row["agent_instance_id"] if row["scope"] == "agent" else None
    predicates = [
        MemoryItemRecord.owner_user_id == row["owner_user_id"],
        MemoryItemRecord.scope == row["scope"],
        MemoryItemRecord.agent_instance_id == agent,
    ]
    duplicate = conn.scalar(
        select(MemoryItemRecord.id)
        .join(MemoryVersionRecord, MemoryVersionRecord.memory_item_id == MemoryItemRecord.id)
        .where(
            *predicates,
            MemoryItemRecord.status == "active",
            MemoryVersionRecord.version == MemoryItemRecord.current_version,
            MemoryVersionRecord.content_digest == row["content_digest"],
        )
        .limit(1)
    )
    if duplicate is not None:
        conn.execute(
            update(MemorySuggestionRecord)
            .where(MemorySuggestionRecord.id == row["id"])
            .values(state="duplicate")
        )
        return
    # Automatic retention must not resurrect a fact the owner edited or removed.
    # A fresh explicit approval can intentionally restore it.
    if (
        automatic
        and conn.scalar(
            select(MemoryItemRecord.id)
            .join(MemoryVersionRecord, MemoryVersionRecord.memory_item_id == MemoryItemRecord.id)
            .where(*predicates, MemoryVersionRecord.content_digest == row["content_digest"])
            .limit(1)
        )
        is not None
    ):
        return
    count = conn.scalar(
        select(func.count())
        .select_from(MemoryItemRecord)
        .where(*predicates, MemoryItemRecord.status == "active")
    )
    if int(count or 0) >= 1000:
        conn.execute(
            update(MemorySuggestionRecord)
            .where(MemorySuggestionRecord.id == row["id"])
            .values(state="quota")
        )
        return
    item = conn.execute(
        insert(MemoryItemRecord).values(
            owner_user_id=row["owner_user_id"],
            agent_instance_id=agent,
            scope=row["scope"],
            status="active",
            current_version=1,
            created_at=now,
            updated_at=now,
        )
    )
    primary_key = item.inserted_primary_key
    if primary_key is None or primary_key[0] is None:
        raise IntegrationConflict("memory item could not be allocated")
    item_id = int(primary_key[0])
    conn.execute(
        insert(MemoryVersionRecord).values(
            memory_item_id=item_id,
            version=1,
            content=row["content"],
            content_digest=row["content_digest"],
            source_kind="promoted_run",
            source_id=row["source_run_id"],
            provenance_type="user_approved_inferred",
            created_by_user_id=row["owner_user_id"],
            created_at=now,
        )
    )
    conn.execute(
        update(MemorySuggestionRecord)
        .where(MemorySuggestionRecord.id == row["id"])
        .values(state="saved", memory_item_id=item_id)
    )
