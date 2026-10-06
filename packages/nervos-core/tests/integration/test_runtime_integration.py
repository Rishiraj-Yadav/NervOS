"""ADR 0031: immutable context, owner policy and durable memory retention."""

from datetime import timedelta
from pathlib import Path

import pytest
from execution_support import NOW, TEST_CAPABILITY, migrate
from nervos_core.application.runtime_integration import (
    IntegrationConflict,
    IntegrationNotFound,
    MemoryProposal,
)
from nervos_core.application.sandbox import WorkerSandboxCapability
from nervos_core.domain.memory import (
    MemoryProvenanceType,
    MemoryScope,
    MemorySourceKind,
    compute_memory_digest,
)
from nervos_core.domain.runs import ModelUsage, Run, RunLimits
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobExecutionPersistence,
    SqlAlchemyJobPersistence,
)
from nervos_core.infrastructure.database.memory import SqlAlchemyMemoryPersistence
from nervos_core.infrastructure.database.models import (
    ConversationRecord,
    ConversationRunLinkRecord,
    ConversationTurnRecord,
    MemoryItemRecord,
    RunIntegrationRecord,
    RunRecord,
)
from nervos_core.infrastructure.database.runtime_integration import (
    SqlAlchemyRuntimeIntegrationPersistence,
    eligible_conversation_source,
)
from sqlalchemy import Engine, func, insert, select, update


def _submit(engine: Engine, text: str = "Remember my preference for concise answers") -> Run:
    return SqlAlchemyJobPersistence(engine).submit(
        owner_user_id=1, agent_instance_id=1, input_text=text, limits=RunLimits(), now=NOW
    )


def _finish(engine: Engine, run: Run, proposals: tuple[MemoryProposal, ...] = ()) -> None:
    store = SqlAlchemyJobExecutionPersistence(engine)
    worker = f"worker-{run.id}"
    moment = run.created_at + timedelta(seconds=1)
    store.register_worker(worker_id=worker, capability=TEST_CAPABILITY, now=moment)
    claim = store.claim_next(
        worker_id=worker,
        provider_ids=("anthropic",),
        max_active=4,
        now=moment,
        lease_duration=timedelta(seconds=60),
    )
    assert claim is not None and claim.run_id == run.id
    assert store.start_attempt(claim, now=moment)
    assert store.succeed(
        claim,
        output_text="A concise answer.",
        finish_reason="stop",
        usage=ModelUsage(),
        elapsed_ms=1,
        now=moment + timedelta(seconds=1),
        memory_proposals=proposals,
    )


def test_automatic_private_memory_is_durable_idempotent_and_used_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "memory.db", monkeypatch)
    integration = SqlAlchemyRuntimeIntegrationPersistence(engine)
    integration.set_policy(1, 1, "automatic_private", False, 0, NOW)
    run = _submit(engine)
    _finish(engine, run, (MemoryProposal("User prefers concise answers."),))
    # Obligation survived primary success; an independently composed service recovers it.
    restarted = SqlAlchemyRuntimeIntegrationPersistence(engine)
    restarted.maintain_memory(NOW + timedelta(seconds=2))
    restarted.maintain_memory(NOW + timedelta(seconds=3))
    suggestions = restarted.suggestions(1)["items"]
    assert len(suggestions) == 1 and suggestions[0]["state"] == "saved"
    future = _submit(engine, "Explain SQLite")
    snapshot = SqlAlchemyJobExecutionPersistence(engine).load_run_context_snapshot(future.id)
    assert snapshot is not None
    assert snapshot.current_user_text == "Explain SQLite"
    assert len(snapshot.selected_memories) == 1
    assert "concise answers" in snapshot.rendered_context
    assert restarted.run_context(1, run.id)["memories"] == []
    engine.dispose()


@pytest.mark.parametrize("mode", ["manual", "review", "automatic_private"])
def test_user_scope_never_saves_automatically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    engine = migrate(tmp_path / "scope.db", monkeypatch)
    integration = SqlAlchemyRuntimeIntegrationPersistence(engine)
    integration.set_policy(1, 1, mode, False, 0, NOW)
    run = _submit(engine)
    _finish(engine, run, (MemoryProposal("User prefers concise answers.", "user"),))
    integration.maintain_memory(NOW + timedelta(seconds=2))
    suggestions = integration.suggestions(1)["items"]
    if mode == "manual":
        assert suggestions == []
    else:
        assert suggestions[0]["state"] == "pending"
        integration.decide_suggestion(1, suggestions[0]["id"], True, NOW + timedelta(seconds=3))
        assert integration.suggestions(1)["items"][0]["state"] == "saved"
    engine.dispose()


def test_policy_revocation_blocks_pending_writes_and_old_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "revocation.db", monkeypatch)
    integration = SqlAlchemyRuntimeIntegrationPersistence(engine)
    integration.set_policy(1, 1, "automatic_private", False, 0, NOW)
    run = _submit(engine)
    _finish(engine, run, (MemoryProposal("User likes Python."),))
    integration.set_policy(1, 1, "manual", False, 1, NOW + timedelta(seconds=2))
    integration.maintain_memory(NOW + timedelta(seconds=3))
    assert integration.suggestions(1)["items"] == []
    assert integration.run_context(1, run.id)["memory_state"] == "policy_changed"
    with pytest.raises(IntegrationConflict):
        integration.set_policy(1, 1, "review", False, 1, NOW)
    engine.dispose()


def test_memory_eligibility_excludes_deleted_and_superseded_conversation_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "conversation-eligibility.db", monkeypatch)
    primary = _submit(engine)
    retry = _submit(engine, "retry")
    with engine.begin() as conn:
        conversation_id = conn.execute(
            insert(ConversationRecord)
            .values(
                owner_user_id=1,
                agent_instance_id=1,
                status="deleted",
                deleted_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
            .returning(ConversationRecord.id)
        ).scalar_one()
        turn_id = conn.execute(
            insert(ConversationTurnRecord)
            .values(
                conversation_id=conversation_id,
                sequence=1,
                state="succeeded",
                client_message_id="memory-eligibility",
                content_digest=b"x" * 32,
                authoritative_run_id=primary.id,
                created_at=NOW,
                started_at=NOW,
                finished_at=NOW,
            )
            .returning(ConversationTurnRecord.id)
        ).scalar_one()
        conn.execute(
            insert(ConversationRunLinkRecord).values(
                turn_id=turn_id,
                run_id=primary.id,
                ordinal=1,
                role="initial",
                context_mode="f2_context_snapshot",
                created_at=NOW,
            )
        )
        conn.execute(
            insert(ConversationRunLinkRecord).values(
                turn_id=turn_id,
                run_id=retry.id,
                ordinal=2,
                role="retry",
                context_mode="f2_context_snapshot",
                created_at=NOW,
            )
        )
        assert not eligible_conversation_source(conn, primary.id)
        conn.execute(
            update(ConversationRecord)
            .where(ConversationRecord.id == conversation_id)
            .values(status="active", deleted_at=None)
        )
        assert eligible_conversation_source(conn, primary.id)
        assert not eligible_conversation_source(conn, retry.id)
    engine.dispose()


def test_deleted_automatic_fact_is_not_resurrected_without_new_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "deleted-fact.db", monkeypatch)
    memory = SqlAlchemyMemoryPersistence(engine)
    existing = memory.create_memory(
        owner_user_id=1,
        scope=MemoryScope.AGENT,
        agent_instance_id=1,
        content="User prefers concise answers.",
        content_digest=compute_memory_digest("User prefers concise answers."),
        source_kind=MemorySourceKind.DIRECT_USER,
        source_id=None,
        provenance_type=MemoryProvenanceType.USER_AUTHORED,
        now=NOW,
    )
    memory.delete_memory(
        owner_user_id=1, memory_item_id=existing.item.id, expected_version=1, now=NOW
    )
    integration = SqlAlchemyRuntimeIntegrationPersistence(engine)
    integration.set_policy(1, 1, "automatic_private", False, 0, NOW)
    run = _submit(engine)
    _finish(engine, run, (MemoryProposal("User prefers concise answers."),))
    integration.maintain_memory(NOW + timedelta(seconds=2))
    suggestions = integration.suggestions(1)["items"]
    assert len(suggestions) == 1 and suggestions[0]["state"] == "pending"
    integration.decide_suggestion(1, suggestions[0]["id"], True, NOW + timedelta(seconds=3))
    assert integration.suggestions(1)["items"][0]["state"] == "saved"
    engine.dispose()


def test_context_keeps_old_memory_version_after_edit_and_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "snapshot.db", monkeypatch)
    memory = SqlAlchemyMemoryPersistence(engine)
    item = memory.create_memory(
        owner_user_id=1,
        scope=MemoryScope.AGENT,
        agent_instance_id=1,
        content="User likes Python.",
        content_digest=compute_memory_digest("User likes Python."),
        source_kind=MemorySourceKind.DIRECT_USER,
        source_id=None,
        provenance_type=MemoryProvenanceType.USER_AUTHORED,
        now=NOW,
    )
    run = _submit(engine)
    memory.edit_memory(
        owner_user_id=1,
        memory_item_id=item.item.id,
        expected_version=1,
        content="User likes Rust.",
        content_digest=compute_memory_digest("User likes Rust."),
        now=NOW + timedelta(seconds=1),
    )
    memory.delete_memory(
        owner_user_id=1,
        memory_item_id=item.item.id,
        expected_version=2,
        now=NOW + timedelta(seconds=2),
    )
    before = SqlAlchemyRuntimeIntegrationPersistence(engine).run_context(1, run.id)
    assert "User likes Python." in before["rendered_context"]
    after = _submit(engine)
    assert (
        SqlAlchemyRuntimeIntegrationPersistence(engine).run_context(1, after.id)["memories"] == []
    )
    engine.dispose()


def test_owner_scoping_and_safe_health_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "ownership.db", monkeypatch)
    run = _submit(engine)
    integration = SqlAlchemyRuntimeIntegrationPersistence(engine)
    for lookup in (
        lambda: integration.policy(2, 1),
        lambda: integration.agent_tools(2, 1),
        lambda: integration.run_context(2, run.id),
        lambda: integration.set_policy(2, 1, "automatic_private", False, 0, NOW),
    ):
        with pytest.raises(IntegrationNotFound):
            lookup()
    health = integration.health(2, NOW)
    assert set(health) == {
        "observed_at",
        "execution_available",
        "package_sandbox",
        "owner_jobs",
    }
    assert health["owner_jobs"] == {}
    engine.dispose()


def test_health_reports_only_live_worker_sandbox_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The projection is Worker-reported observed health, bounded and free of identity."""
    engine = migrate(tmp_path / "sandbox-health.db", monkeypatch)
    integration = SqlAlchemyRuntimeIntegrationPersistence(engine)
    store = SqlAlchemyJobExecutionPersistence(engine)

    assert integration.health(1, NOW)["package_sandbox"] == {
        "supported": False,
        "platform": "unknown",
        "backend": None,
        "reason": "No execution Worker is currently available to report package sandbox support.",
    }

    store.register_worker(
        worker_id="unsupported-worker",
        capability=WorkerSandboxCapability(False, "windows", None),
        now=NOW,
    )
    unsupported = integration.health(1, NOW)["package_sandbox"]
    assert unsupported["supported"] is False
    assert unsupported["platform"] == "windows"
    assert unsupported["backend"] is None
    assert unsupported["reason"] == "Package execution is qualified only on Linux with bubblewrap."
    assert "unsupported-worker" not in str(unsupported)

    store.register_worker(
        worker_id="qualified-worker",
        capability=WorkerSandboxCapability(True, "linux", "bubblewrap"),
        now=NOW,
    )
    qualified = integration.health(1, NOW)["package_sandbox"]
    assert qualified == {
        "supported": True,
        "platform": "linux",
        "backend": "bubblewrap",
        "reason": None,
    }
    assert "qualified-worker" not in str(qualified)

    # Capability is observed health: a Worker that stopped heartbeating no longer counts.
    stale = integration.health(1, NOW + timedelta(seconds=31))["package_sandbox"]
    assert stale["supported"] is False and stale["platform"] == "unknown"
    engine.dispose()


def test_extraction_is_an_ordinary_job_and_cannot_recurse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "extraction.db", monkeypatch)
    integration = SqlAlchemyRuntimeIntegrationPersistence(engine)
    integration.set_policy(1, 1, "automatic_private", True, 0, NOW)
    primary = _submit(engine)
    _finish(engine, primary)
    integration.maintain_memory(NOW + timedelta(seconds=2))
    with engine.connect() as conn:
        derived_id = conn.scalar(
            select(RunIntegrationRecord.extraction_run_id).where(
                RunIntegrationRecord.run_id == primary.id
            )
        )
    assert derived_id is not None and integration.is_extraction_run(derived_id)
    derived = SqlAlchemyJobExecutionPersistence(engine).load_run(derived_id)
    assert derived.limits.max_model_calls == 1 and derived.limits.max_tool_calls == 0
    _finish(engine, derived, (MemoryProposal("User prefers concise answers."),))
    integration.maintain_memory(NOW + timedelta(seconds=3))
    integration.maintain_memory(NOW + timedelta(seconds=4))
    assert integration.suggestions(1)["items"][0]["state"] == "saved"
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(RunRecord)) == 2
        assert conn.scalar(select(func.count()).select_from(MemoryItemRecord)) == 1
    engine.dispose()
