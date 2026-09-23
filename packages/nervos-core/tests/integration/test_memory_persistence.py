"""Integration tests for F3 Memory persistence, retrieval, keyset refill, and 1000-item cap."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from execution_support import migrate
from nervos_core.application.memory import (
    MemoryService,
)
from nervos_core.domain.memory import (
    MemoryNotFound,
    MemoryScope,
    MemoryScopeCapacityExceeded,
)
from nervos_core.infrastructure.database import create_session_factory
from nervos_core.infrastructure.database.agents import SqlAlchemyAgentPersistence
from nervos_core.infrastructure.database.jobs import SqlAlchemyJobPersistence
from nervos_core.infrastructure.database.memory import (
    SqlAlchemyMemoryPersistence,
)
from nervos_core.infrastructure.database.models import (
    AgentInstanceRecord,
    MemoryItemRecord,
    MemoryVersionRecord,
    UserRecord,
)
from sqlalchemy import Engine, insert

NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)


def _seed_user_and_agent(engine: Engine, username: str = "alice") -> tuple[int, int]:
    with engine.begin() as conn:
        u_pk = conn.execute(
            insert(UserRecord).values(
                username=username,
                password_hash="fake-hash",
                role="user",
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        ).inserted_primary_key
        assert u_pk is not None
        user_id = int(u_pk[0])

        pk = conn.execute(
            insert(AgentInstanceRecord).values(
                owner_user_id=user_id,
                agent_key="nervos.chat",
                agent_definition_version="1",
                display_name="Agent",
                enabled=True,
                model_provider="anthropic",
                model_name="opaque/model",
                created_at=NOW,
                updated_at=NOW,
            )
        ).inserted_primary_key
        assert pk is not None
        inst_id = int(pk[0])
    return user_id, inst_id


def _setup_service(engine: Engine) -> MemoryService:
    from nervos_core.application.agent_definitions import (
        create_builtin_definition_registry,
    )
    from nervos_core.application.agents import AgentService
    from nervos_core.application.model_providers import ModelProviderCatalog

    session_factory = create_session_factory(engine)
    known_providers = ModelProviderCatalog([], known=("anthropic", "openai"))
    agents = AgentService(
        SqlAlchemyAgentPersistence(session_factory),
        create_builtin_definition_registry(),
        lambda: NOW,
        known_providers,
        SqlAlchemyJobPersistence(engine),
    )
    persistence = SqlAlchemyMemoryPersistence(engine, sleep=lambda _: None)
    return MemoryService(persistence, agents, clock=lambda: NOW)


def test_keyset_candidate_retrieval_and_refill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = migrate(tmp_path / "refill_test.db", monkeypatch)
    user_id, agent_id = _seed_user_and_agent(db)
    service = _setup_service(db)

    # Insert 10 USER items and 40 AGENT items
    for i in range(1, 11):
        service.create_memory(
            owner_user_id=user_id,
            scope=MemoryScope.USER,
            content=f"User Fact {i}",
        )
    for i in range(1, 41):
        service.create_memory(
            owner_user_id=user_id,
            scope=MemoryScope.AGENT,
            content=f"Agent Fact {i}",
            agent_instance_id=agent_id,
        )

    # Retrieval: candidate limit 50.
    # USER scope has only 10 (<25). The remaining 15 slots must be refilled from AGENT scope!
    # Result should have exactly 10 USER items + 40 AGENT items = 50 items total!
    candidates = service.retrieve_candidates(user_id, agent_id, limit=50)
    assert len(candidates) == 50

    user_candidates = [c for c in candidates if c.scope is MemoryScope.USER]
    agent_candidates = [c for c in candidates if c.scope is MemoryScope.AGENT]

    assert len(user_candidates) == 10
    assert len(agent_candidates) == 40

    # Total order: USER items precede AGENT items; within scope, ordered by id DESC
    assert [c.content for c in user_candidates] == [f"User Fact {i}" for i in range(10, 0, -1)]
    assert [c.content for c in agent_candidates] == [f"Agent Fact {i}" for i in range(40, 0, -1)]


def test_active_items_capacity_cap_1000_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = migrate(tmp_path / "cap_test.db", monkeypatch)
    user_id, agent_id = _seed_user_and_agent(db)
    service = _setup_service(db)

    # Bulk insert 1000 items directly into database to simulate reaching cap
    with db.begin() as conn:
        for i in range(1, 1001):
            item_pk = conn.execute(
                insert(MemoryItemRecord).values(
                    owner_user_id=user_id,
                    agent_instance_id=None,
                    scope="user",
                    status="active",
                    current_version=1,
                    created_at=NOW,
                    updated_at=NOW,
                )
            ).inserted_primary_key
            assert item_pk is not None
            conn.execute(
                insert(MemoryVersionRecord).values(
                    memory_item_id=int(item_pk[0]),
                    version=1,
                    content=f"Bulk Fact {i}",
                    content_digest=b"d" * 32,
                    source_kind="direct_user",
                    source_id=None,
                    provenance_type="user_authored",
                    created_by_user_id=user_id,
                    created_at=NOW,
                )
            )

    # The 1001st item creation must raise MemoryScopeCapacityExceeded
    with pytest.raises(MemoryScopeCapacityExceeded, match="capacity exceeded"):
        service.create_memory(
            owner_user_id=user_id,
            scope=MemoryScope.USER,
            content="1001st Fact",
        )

    # But AGENT scope has independent capacity (should succeed)
    agent_mem = service.create_memory(
        owner_user_id=user_id,
        scope=MemoryScope.AGENT,
        content="Agent Fact 1",
        agent_instance_id=agent_id,
    )
    assert agent_mem.item.id is not None


def test_owner_and_agent_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = migrate(tmp_path / "isolation_test.db", monkeypatch)
    user1, agent1 = _seed_user_and_agent(db, "alice")
    user2, agent2 = _seed_user_and_agent(db, "bob")
    service = _setup_service(db)

    service.create_memory(owner_user_id=user1, scope=MemoryScope.USER, content="Alice User Fact")
    service.create_memory(
        owner_user_id=user1,
        scope=MemoryScope.AGENT,
        content="Alice Agent Fact",
        agent_instance_id=agent1,
    )

    # User 2 cannot see User 1's memory
    user2_candidates = service.retrieve_candidates(user2, agent2, limit=50)
    assert len(user2_candidates) == 0

    # User 2 cannot get User 1's memory by ID
    with pytest.raises(MemoryNotFound):
        service.get_memory(owner_user_id=user2, memory_item_id=1)


def test_list_memories_and_versions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = migrate(tmp_path / "list_mem_test.db", monkeypatch)
    user_id, agent_id = _seed_user_and_agent(db)
    service = _setup_service(db)

    m1 = service.create_memory(owner_user_id=user_id, scope=MemoryScope.USER, content="Fact 1")
    m2 = service.create_memory(
        owner_user_id=user_id, scope=MemoryScope.AGENT, content="Fact 2", agent_instance_id=agent_id
    )
    m3 = service.create_memory(owner_user_id=user_id, scope=MemoryScope.USER, content="Fact 3")

    # List all active memories (default 20, newest first: m3, m2, m1)
    items, next_before_id = service.list_memories(owner_user_id=user_id, limit=20)
    assert len(items) == 3
    assert [i.item.id for i in items] == [m3.item.id, m2.item.id, m1.item.id]
    assert next_before_id is None

    # List with pagination limit=2
    items_p1, next_id_p1 = service.list_memories(owner_user_id=user_id, limit=2)
    assert len(items_p1) == 2
    assert [i.item.id for i in items_p1] == [m3.item.id, m2.item.id]
    assert next_id_p1 == m2.item.id

    items_p2, next_id_p2 = service.list_memories(
        owner_user_id=user_id, before_id=next_id_p1, limit=2
    )
    assert len(items_p2) == 1
    assert items_p2[0].item.id == m1.item.id
    assert next_id_p2 is None

    # Filter by scope
    user_items, _ = service.list_memories(owner_user_id=user_id, scope=MemoryScope.USER)
    assert len(user_items) == 2
    assert [i.item.id for i in user_items] == [m3.item.id, m1.item.id]

    agent_items, _ = service.list_memories(
        owner_user_id=user_id, scope=MemoryScope.AGENT, agent_instance_id=agent_id
    )
    assert len(agent_items) == 1
    assert agent_items[0].item.id == m2.item.id


def test_edit_memory_version_allocation_and_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nervos_core.domain.memory import StaleMemoryVersion

    db = migrate(tmp_path / "edit_mem_test.db", monkeypatch)
    user_id, agent_id = _seed_user_and_agent(db)
    service = _setup_service(db)

    mem = service.create_memory(
        owner_user_id=user_id,
        scope=MemoryScope.AGENT,
        content="Initial text",
        agent_instance_id=agent_id,
    )
    assert mem.item.current_version == 1
    assert mem.current_version_record.content == "Initial text"

    # Successful edit with expected_version=1
    edited = service.edit_memory(
        owner_user_id=user_id,
        memory_item_id=mem.item.id,
        expected_version=1,
        content="Updated text v2",
    )
    assert edited.item.current_version == 2
    assert edited.current_version_record.version == 2
    assert edited.current_version_record.content == "Updated text v2"
    assert edited.current_version_record.provenance_type.value == "user_authored"

    # Stale edit with expected_version=1 must fail with StaleMemoryVersion
    with pytest.raises(StaleMemoryVersion, match="mismatch"):
        service.edit_memory(
            owner_user_id=user_id,
            memory_item_id=mem.item.id,
            expected_version=1,
            content="Stale write",
        )

    # Historical versions are inspectable in reverse chronological order
    versions, _ = service.list_memory_versions(owner_user_id=user_id, memory_item_id=mem.item.id)
    assert len(versions) == 2
    assert [v.version for v in versions] == [2, 1]
    assert [v.content for v in versions] == ["Updated text v2", "Initial text"]

    # Active retrieval returns only the new current version (v2)
    candidates = service.retrieve_candidates(user_id, agent_id)
    assert len(candidates) == 1
    assert candidates[0].version == 2
    assert candidates[0].content == "Updated text v2"


def test_delete_memory_and_capacity_freeing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nervos_core.domain.memory import StaleMemoryVersion

    db = migrate(tmp_path / "del_mem_test.db", monkeypatch)
    user_id, agent_id = _seed_user_and_agent(db)
    service = _setup_service(db)

    mem = service.create_memory(
        owner_user_id=user_id, scope=MemoryScope.USER, content="To be deleted"
    )
    mem_id = mem.item.id

    # Stale expected_version on delete raises StaleMemoryVersion
    with pytest.raises(StaleMemoryVersion, match="mismatch"):
        service.delete_memory(owner_user_id=user_id, memory_item_id=mem_id, expected_version=999)

    # Successful delete with matching expected_version
    service.delete_memory(owner_user_id=user_id, memory_item_id=mem_id, expected_version=1)

    # Deleted item is excluded from active get_memory
    with pytest.raises(MemoryNotFound):
        service.get_memory(owner_user_id=user_id, memory_item_id=mem_id)

    # Deleted item is excluded from active list
    items, _ = service.list_memories(owner_user_id=user_id)
    assert len(items) == 0

    # Deleted item is excluded from candidate retrieval
    candidates = service.retrieve_candidates(user_id, agent_id)
    assert len(candidates) == 0

    # Deleted item cannot be edited
    with pytest.raises(MemoryNotFound):
        service.edit_memory(
            owner_user_id=user_id,
            memory_item_id=mem_id,
            expected_version=1,
            content="Should not work",
        )

    # Repeated delete is idempotent
    service.delete_memory(owner_user_id=user_id, memory_item_id=mem_id)
