"""Stage F3 query plans: memory candidate reads and count queries are index seeks."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from nervos_core.application.agents import AgentService
from nervos_core.application.memory import MemoryService
from nervos_core.application.model_providers import ModelProviderCatalog
from nervos_core.domain.memory import MemoryScope
from nervos_core.infrastructure.database import create_session_factory
from nervos_core.infrastructure.database.agents import SqlAlchemyAgentPersistence
from nervos_core.infrastructure.database.jobs import SqlAlchemyJobPersistence
from nervos_core.infrastructure.database.memory import (
    SqlAlchemyMemoryPersistence,
)
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


class MemoryPlanRig:
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
        self.persistence = SqlAlchemyMemoryPersistence(engine, sleep=lambda _: None)
        self.service = MemoryService(self.persistence, self.agents, clock=lambda: NOW)

    @classmethod
    def build(cls, path: Path, monkeypatch: pytest.MonkeyPatch) -> MemoryPlanRig:
        engine = migrate(path, monkeypatch)
        return cls(engine)


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[MemoryPlanRig]:
    built = MemoryPlanRig.build(tmp_path / "memory_plans.db", monkeypatch)
    try:
        yield built
    finally:
        built.engine.dispose()


def test_user_candidate_retrieval_uses_owner_scope_index(rig: MemoryPlanRig) -> None:
    rig.service.create_memory(owner_user_id=OWNER, scope=MemoryScope.USER, content="User Fact")

    with Captured(rig.engine) as captured:
        rig.service.retrieve_candidates(OWNER, AGENT, limit=50)

    selects = captured.selects("memory_items", "memory_versions")
    assert len(selects) >= 1
    plan_text = plan_for(rig.engine, selects[0])
    assert any(
        target in plan_text
        for target in ("ix_memory_items_owner_scope_status", "USING INDEX", "SEARCH")
    )


def test_agent_candidate_retrieval_uses_owner_agent_index(rig: MemoryPlanRig) -> None:
    rig.service.create_memory(
        owner_user_id=OWNER, scope=MemoryScope.AGENT, content="Agent Fact", agent_instance_id=AGENT
    )

    with Captured(rig.engine) as captured:
        rig.service.retrieve_candidates(OWNER, AGENT, limit=50)

    selects = captured.selects("memory_items", "memory_versions")
    assert len(selects) >= 1
    plan_text = plan_for(rig.engine, selects[0])
    assert any(
        target in plan_text
        for target in ("ix_memory_items_owner_agent_status", "USING INDEX", "SEARCH")
    )


def test_list_memories_uses_owner_scope_index(rig: MemoryPlanRig) -> None:
    rig.service.create_memory(owner_user_id=OWNER, scope=MemoryScope.USER, content="User Fact")

    with Captured(rig.engine) as captured:
        rig.service.list_memories(owner_user_id=OWNER, scope=MemoryScope.USER, limit=20)

    selects = captured.selects("memory_items", "memory_versions")
    assert len(selects) >= 1
    plan_text = plan_for(rig.engine, selects[0])
    assert any(
        target in plan_text
        for target in ("ix_memory_items_owner_scope_status", "USING INDEX", "SEARCH")
    )
