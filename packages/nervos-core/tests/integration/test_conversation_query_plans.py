"""Stage F1 query plans: conversation hot-path reads are index seeks over `0009`'s indexes."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from nervos_core.application.agents import AgentService
from nervos_core.application.conversations import ConversationService
from nervos_core.application.model_providers import ModelProviderCatalog
from nervos_core.infrastructure.database import create_session_factory
from nervos_core.infrastructure.database.agents import SqlAlchemyAgentPersistence
from nervos_core.infrastructure.database.conversations import (
    SqlAlchemyConversationPersistence,
)
from nervos_core.infrastructure.database.jobs import SqlAlchemyJobPersistence
from scheduler_support import AGENT, NOW, OWNER, migrate
from sqlalchemy import Engine, event


class Captured:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self.statements: list[tuple[str, Any]] = []

    def __enter__(self) -> Captured:
        def record(
            conn: Any,
            cursor: Any,
            statement: str,
            parameters: Any,
            context: Any,
            executemany: bool,
        ) -> None:
            del conn, cursor, context, executemany
            self.statements.append((statement, parameters))

        self._handler = record
        event.listen(self._engine, "before_cursor_execute", record)
        return self

    def __exit__(self, *_: object) -> None:
        event.remove(self._engine, "before_cursor_execute", self._handler)

    def selects(self, *needles: str) -> list[tuple[str, Any]]:
        return [
            (statement, parameters)
            for statement, parameters in self.statements
            if statement.lstrip().upper().startswith("SELECT")
            and all(needle in statement for needle in needles)
        ]


def plan_for(engine: Engine, captured: tuple[str, Any]) -> str:
    statement, parameters = captured
    with engine.connect() as connection:
        rows = connection.exec_driver_sql(f"EXPLAIN QUERY PLAN {statement}", parameters).all()
    return " | ".join(str(row[-1]) for row in rows)


class PlanRig:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        session_factory = create_session_factory(engine)
        from nervos_core.application.agent_definitions import (
            create_builtin_definition_registry,
        )

        known_providers = ModelProviderCatalog([], known=("anthropic", "openai"))
        self.agents = AgentService(
            SqlAlchemyAgentPersistence(session_factory),
            create_builtin_definition_registry(),
            lambda: NOW,
            known_providers,
            SqlAlchemyJobPersistence(engine),
        )
        self.conv_persistence = SqlAlchemyConversationPersistence(engine, sleep=lambda _: None)
        self.service = ConversationService(
            self.conv_persistence,
            self.agents,
            clock=lambda: NOW,
        )

    @classmethod
    def build(cls, path: Path, monkeypatch: pytest.MonkeyPatch) -> PlanRig:
        engine = migrate(path, monkeypatch)
        return cls(engine)


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[PlanRig]:
    built = PlanRig.build(tmp_path / "plans.db", monkeypatch)
    try:
        yield built
    finally:
        built.engine.dispose()


def test_conversation_list_query_uses_owner_index(rig: PlanRig) -> None:
    rig.service.create_conversation(OWNER, AGENT, "C1")
    rig.service.create_conversation(OWNER, AGENT, "C2")

    with Captured(rig.engine) as captured:
        rig.service.list_conversations(OWNER, limit=10)

    selects = captured.selects("conversations")
    assert len(selects) >= 1
    plan_text = plan_for(rig.engine, selects[0])
    assert (
        "ix_conversations_owner_id" in plan_text
        or "USING INDEX" in plan_text
        or "COVERING" in plan_text
    )


def test_turn_history_query_uses_sequence_index(rig: PlanRig) -> None:
    conv = rig.service.create_conversation(OWNER, AGENT, "History Conv")
    rig.service.send_message(OWNER, conv.id, "cmid-1", "Turn 1")

    with Captured(rig.engine) as captured:
        rig.service.list_turns(OWNER, conv.id, limit=10)

    selects = captured.selects("conversation_turns")
    assert len(selects) >= 1
    plan_text = plan_for(rig.engine, selects[0])
    assert (
        "ix_conversation_turns" in plan_text
        or "uq_conversation_turns" in plan_text
        or "USING INDEX" in plan_text
    )


def test_snapshot_lookup_uses_primary_key(rig: PlanRig) -> None:
    conv = rig.service.create_conversation(OWNER, AGENT, "Snap Conv")
    detail, _ = rig.service.send_message(OWNER, conv.id, "cmid-1", "Query")
    run_id = detail.latest_run_id
    assert run_id is not None

    with Captured(rig.engine) as captured:
        rig.conv_persistence.load_run_context_snapshot(run_id)

    selects = captured.selects("run_context_snapshots")
    assert len(selects) >= 1
    plan_text = plan_for(rig.engine, selects[0])
    assert any(
        target in plan_text for target in ("PRIMARY KEY", "pk_run_context_snapshots", "SEARCH")
    )


def test_current_compaction_lookup_uses_partial_index(rig: PlanRig) -> None:
    conv = rig.service.create_conversation(OWNER, AGENT, "Compaction Conv")
    with Captured(rig.engine) as captured:
        rig.service.send_message(OWNER, conv.id, "cmid-1", "Msg 1")

    selects = captured.selects("conversation_compactions")
    assert len(selects) >= 1
    plan_text = plan_for(rig.engine, selects[0])
    assert any(
        target in plan_text
        for target in ("uq_conversation_compactions_one_current", "USING INDEX", "SEARCH")
    )
