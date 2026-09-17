"""D3 durable built-in registration: reconciliation, identity stability, and what must not change.

Reconciliation makes the three built-in definitions durable and idempotent. It never touches
``agent_tool_grants``: a material change is *supposed* to move the definition's fingerprint away
from the stored ``reviewed_fingerprint``, and D2's already-accepted evaluator then denies the call
until the user re-confirms. Nothing here rewrites a reviewed fingerprint, renames a durable
model-facing name, or grants anything.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from execution_support import migrate
from nervos_core.application.builtin_tools import (
    BuiltinToolSource,
    BuiltinToolSpec,
    builtin_tool_specs,
    canonical_spec_schemas,
    reconcile_builtin_definitions,
)
from nervos_core.application.errors import PersistenceUnavailable
from nervos_core.application.tool_permissions import PermissionDenialReason
from nervos_core.application.tool_registry import ToolDefinitionMaterial
from nervos_core.domain.tools import (
    DefinitionStatus,
    InvalidToolDefinition,
    ToolDescriptor,
    ToolSourceKind,
    ToolSourceRef,
)
from nervos_core.infrastructure.database.models import (
    AgentToolGrantRecord,
    RunRecord,
    ToolDefinitionRecord,
)
from nervos_core.infrastructure.database.tool_definitions import (
    SqlAlchemyToolDefinitionPersistence,
)
from nervos_core.infrastructure.database.tools import (
    SqlAlchemyToolPermissionEvaluator,
    SqlAlchemyToolPermissionPersistence,
)
from sqlalchemy import Engine, insert, select
from sqlalchemy.exc import IntegrityError

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
BUILTIN_REF = ToolSourceRef(ToolSourceKind.BUILTIN, None)
SPECS = builtin_tool_specs(clock=lambda: NOW)


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    return migrate(tmp_path / "tool_definitions.db", monkeypatch, agents=1)


@pytest.fixture
def persistence(engine: Engine) -> SqlAlchemyToolDefinitionPersistence:
    return SqlAlchemyToolDefinitionPersistence(engine)


def reconcile(
    engine: Engine, specs: Sequence[BuiltinToolSpec] = SPECS, *, now: datetime = NOW
) -> tuple[ToolDescriptor, ...]:
    return reconcile_builtin_definitions(
        SqlAlchemyToolDefinitionPersistence(engine),
        specs=specs,
        now=now,
    )


def rows(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                select(
                    ToolDefinitionRecord.id,
                    ToolDefinitionRecord.source_kind,
                    ToolDefinitionRecord.source_id,
                    ToolDefinitionRecord.upstream_name,
                    ToolDefinitionRecord.model_name,
                    ToolDefinitionRecord.description,
                    ToolDefinitionRecord.fingerprint,
                    ToolDefinitionRecord.status,
                    ToolDefinitionRecord.created_at,
                    ToolDefinitionRecord.updated_at,
                ).order_by(ToolDefinitionRecord.id)
            ).mappings()
        ]


def grant_rows(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                select(
                    AgentToolGrantRecord.id,
                    AgentToolGrantRecord.agent_instance_id,
                    AgentToolGrantRecord.tool_definition_id,
                    AgentToolGrantRecord.reviewed_fingerprint,
                ).order_by(AgentToolGrantRecord.id)
            ).mappings()
        ]


def _insert_run(engine: Engine, *, agent_instance_id: int, tool_grant_cutoff_id: int) -> int:
    """Insert one Run and return its durable id. Mirrors the accepted D2 test helper."""
    with engine.begin() as connection:
        connection.execute(
            insert(RunRecord).values(
                agent_instance_id=agent_instance_id,
                status="created",
                agent_key="nervos.chat",
                agent_definition_version="1",
                model_provider="anthropic",
                model_name="claude-test",
                input_text="tool definition registration",
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
    with engine.connect() as connection:
        return int(
            connection.execute(
                select(RunRecord.id).where(RunRecord.input_text == "tool definition registration")
            ).scalar_one()
        )


class TestFirstReconciliation:
    def test_it_creates_exactly_the_three_builtins(self, engine: Engine) -> None:
        descriptors = reconcile(engine)
        assert [descriptor.upstream_name for descriptor in descriptors] == [
            "current_time",
            "calculate",
            "json_transform",
        ]
        assert len(rows(engine)) == 3

    def test_every_row_is_a_builtin_with_no_source_id_and_available(self, engine: Engine) -> None:
        reconcile(engine)
        for row in rows(engine):
            assert row["source_kind"] == "builtin"
            assert row["source_id"] is None
            assert row["status"] == "available"

    def test_it_returns_durable_descriptors_with_real_ids(self, engine: Engine) -> None:
        descriptors = reconcile(engine)
        stored = {int(row["id"]) for row in rows(engine)}
        for descriptor in descriptors:
            assert descriptor.tool_definition_id > 0
            assert descriptor.tool_definition_id in stored
            assert descriptor.source_ref == BUILTIN_REF

    def test_the_source_exposes_the_same_durable_descriptors(self, engine: Engine) -> None:
        reconcile(engine)
        source = BuiltinToolSource(SqlAlchemyToolDefinitionPersistence(engine))
        listed = asyncio.run(source.list_tools())
        assert [descriptor.upstream_name for descriptor in listed] == [
            "current_time",
            "calculate",
            "json_transform",
        ]
        for descriptor in listed:
            assert descriptor.tool_definition_id > 0

    def test_no_agent_grant_is_created(self, engine: Engine) -> None:
        reconcile(engine)
        assert grant_rows(engine) == []

    def test_the_persisted_model_name_is_provider_safe_and_suffixed(self, engine: Engine) -> None:
        reconcile(engine)
        for row in rows(engine):
            name = str(row["model_name"])
            assert name.startswith("nervos__builtin__")
            assert len(name) <= 64
            assert name == name.lower()


class TestIdempotency:
    def test_a_repeated_reconciliation_writes_nothing(self, engine: Engine) -> None:
        first = reconcile(engine)
        before = rows(engine)
        second = reconcile(engine, now=LATER)
        assert rows(engine) == before
        assert [descriptor.tool_definition_id for descriptor in second] == [
            descriptor.tool_definition_id for descriptor in first
        ]

    def test_a_repeated_reconciliation_does_not_churn_updated_at(self, engine: Engine) -> None:
        reconcile(engine)
        stamped = [row["updated_at"] for row in rows(engine)]
        reconcile(engine, now=LATER)
        assert [row["updated_at"] for row in rows(engine)] == stamped

    def test_reconciling_a_subset_leaves_the_others_untouched(self, engine: Engine) -> None:
        reconcile(engine)
        before = {str(row["upstream_name"]): row for row in rows(engine)}
        reconcile(engine, [SPECS[0]], now=LATER)
        after = {str(row["upstream_name"]): row for row in rows(engine)}
        assert after == before


class TestMaterialChange:
    def test_a_changed_description_updates_the_fingerprint(self, engine: Engine) -> None:
        reconcile(engine)
        before = {str(row["upstream_name"]): row for row in rows(engine)}
        changed = tuple(
            replace(spec, description="A different description.")
            if spec.upstream_name == "calculate"
            else spec
            for spec in SPECS
        )
        reconcile(engine, changed, now=LATER)
        after = {str(row["upstream_name"]): row for row in rows(engine)}

        assert after["calculate"]["fingerprint"] != before["calculate"]["fingerprint"]
        assert after["calculate"]["description"] == "A different description."
        assert after["calculate"]["updated_at"] != before["calculate"]["updated_at"]
        # The other two are untouched.
        assert after["current_time"] == before["current_time"]
        assert after["json_transform"] == before["json_transform"]

    def test_the_persisted_model_name_survives_reconciliation_unchanged(
        self, engine: Engine
    ) -> None:
        reconcile(engine)
        original = {str(row["upstream_name"]): str(row["model_name"]) for row in rows(engine)}
        changed = tuple(
            replace(spec, description="Rewritten.", display_name="Renamed for humans")
            for spec in SPECS
        )
        descriptors = reconcile(engine, changed, now=LATER)

        after = {str(row["upstream_name"]): str(row["model_name"]) for row in rows(engine)}
        assert after == original
        # The identity returned to callers is the persisted one too.
        assert {descriptor.upstream_name: descriptor.model_name for descriptor in descriptors} == (
            original
        )

    def test_a_changed_upstream_name_is_a_new_identity_not_a_rename(self, engine: Engine) -> None:
        reconcile(engine)
        before = rows(engine)
        renamed = tuple(
            replace(spec, upstream_name="calculate_v2")
            if spec.upstream_name == "calculate"
            else spec
            for spec in SPECS
        )
        reconcile(engine, renamed, now=LATER)

        after = rows(engine)
        assert len(after) == 4
        # The original row is retained, byte-identical, and still carries its own name.
        assert after[:3] == before
        assert after[3]["upstream_name"] == "calculate_v2"
        assert after[3]["model_name"] != before[1]["model_name"]

    def test_a_risk_hint_change_alone_moves_the_fingerprint(self, engine: Engine) -> None:
        reconcile(engine)
        before = {str(row["upstream_name"]): row for row in rows(engine)}
        hinted = tuple(
            replace(spec, risk_hints=replace(spec.risk_hints, destructive=True))
            if spec.upstream_name == "calculate"
            else spec
            for spec in SPECS
        )
        reconcile(engine, hinted, now=LATER)
        after = {str(row["upstream_name"]): row for row in rows(engine)}
        assert after["calculate"]["fingerprint"] != before["calculate"]["fingerprint"]


class TestInteractionWithAcceptedD2Authority:
    def grant_calculate(self, engine: Engine) -> int:
        reconcile(engine)
        tool_definition_id = int(
            next(row["id"] for row in rows(engine) if row["upstream_name"] == "calculate")
        )
        SqlAlchemyToolPermissionPersistence(engine).grant_tool(
            owner_user_id=1,
            agent_instance_id=1,
            tool_definition_id=tool_definition_id,
            now=NOW,
        )
        return tool_definition_id

    def test_a_material_change_suspends_the_grant_without_touching_it(self, engine: Engine) -> None:
        tool_definition_id = self.grant_calculate(engine)
        granted = grant_rows(engine)
        assert len(granted) == 1
        cutoff = int(granted[0]["id"])
        run_id = _insert_run(engine, agent_instance_id=1, tool_grant_cutoff_id=cutoff)

        evaluator = SqlAlchemyToolPermissionEvaluator(engine)
        assert evaluator.check_permission(
            run_id=run_id, tool_definition_id=tool_definition_id
        ).allowed

        changed = tuple(
            replace(spec, description="A different description.")
            if spec.upstream_name == "calculate"
            else spec
            for spec in SPECS
        )
        reconcile(engine, changed, now=LATER)

        # The reviewed fingerprint is byte-identical: reconciliation never rewrites it.
        assert grant_rows(engine) == granted
        decision = evaluator.check_permission(run_id=run_id, tool_definition_id=tool_definition_id)
        assert decision.allowed is False
        assert decision.reason is PermissionDenialReason.DEFINITION_CHANGED

    def test_reconciliation_never_creates_or_removes_a_grant(self, engine: Engine) -> None:
        self.grant_calculate(engine)
        granted = grant_rows(engine)
        reconcile(engine, tuple(reversed(SPECS)), now=LATER)
        assert grant_rows(engine) == granted


class TestRefusals:
    def test_an_unsupported_builtin_schema_fails_loudly_and_writes_nothing(
        self, engine: Engine
    ) -> None:
        broken = tuple(
            replace(
                spec,
                input_schema={
                    "type": "object",
                    "properties": {"a": {"type": "string", "$ref": "#/x"}},
                },
            )
            for spec in SPECS
            if spec.upstream_name == "calculate"
        )
        with pytest.raises(InvalidToolDefinition) as caught:
            reconcile(engine, broken)
        assert "unsupported" in str(caught.value)
        # A NervOS-owned built-in is never persisted as `unsupported_schema`: it is our own defect.
        assert rows(engine) == []

    def test_a_builtin_is_never_stored_as_unsupported_schema(self, engine: Engine) -> None:
        reconcile(engine)
        assert {str(row["status"]) for row in rows(engine)} == {DefinitionStatus.AVAILABLE.value}

    def test_a_duplicate_builtin_identity_is_rejected_by_the_database(
        self, engine: Engine, persistence: SqlAlchemyToolDefinitionPersistence
    ) -> None:
        reconcile(engine)
        # SQLite does not enforce uniqueness over a NULL, so the built-in case is guarded by a
        # partial unique index. A non-contention failure surfaces as PersistenceUnavailable with the
        # underlying IntegrityError preserved as its cause.
        with pytest.raises(PersistenceUnavailable) as caught:
            persistence.insert(
                ToolDefinitionMaterial(
                    source_kind=ToolSourceKind.BUILTIN,
                    source_id=None,
                    upstream_name="calculate",
                    model_name="nervos__builtin__duplicate_0123456789ab",
                    display_name="Duplicate",
                    description="Another calculate",
                    input_schema='{"type":"object","properties":{}}',
                    output_schema=None,
                    fingerprint="a" * 64,
                    status=DefinitionStatus.AVAILABLE,
                    risk_hints=SPECS[0].risk_hints,
                ),
                now=NOW,
            )
        assert isinstance(caught.value.__cause__, IntegrityError)

    def test_the_partial_index_lets_distinct_builtin_identities_coexist(
        self, engine: Engine
    ) -> None:
        reconcile(engine)
        assert len(rows(engine)) == 3


class TestBuiltinSpecsAreSelfConsistent:
    def test_every_spec_schema_is_inside_the_canonical_subset(self) -> None:
        for spec in SPECS:
            input_schema, output_schema = canonical_spec_schemas(spec)
            assert input_schema.node_count >= 1
            if output_schema is not None:
                assert output_schema.node_count >= 1

    def test_the_source_ref_is_the_builtin_one(self) -> None:
        assert BUILTIN_REF.is_builtin is True
        assert BUILTIN_REF.source_id is None
