"""F1 Conversation integration tests.

Covers creation, send, idempotency, retry, finalization, recovery, tool execution.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from d6_support import Rig as DRig
from nervos_core.application.agents import AgentService
from nervos_core.application.builtin_tools import (
    builtin_tool_specs,
    reconcile_builtin_definitions,
)
from nervos_core.application.conversations import (
    ConversationBusy,
    ConversationConflict,
    ConversationNotFound,
    ConversationService,
    TurnNotRetryable,
)
from nervos_core.application.model_completion import EXECUTION_OUTCOME_AMBIGUOUS
from nervos_core.application.model_providers import ModelProviderCatalog
from nervos_core.domain.conversations import (
    MessageRole,
    TurnState,
)
from nervos_core.infrastructure.database import create_session_factory
from nervos_core.infrastructure.database.agents import SqlAlchemyAgentPersistence
from nervos_core.infrastructure.database.conversations import (
    SqlAlchemyConversationPersistence,
)
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobExecutionPersistence,
    SqlAlchemyJobPersistence,
)
from nervos_core.infrastructure.database.tool_definitions import (
    SqlAlchemyToolDefinitionPersistence,
)
from scheduler_support import AGENT, NOW, OTHER_OWNER, OWNER, migrate
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from stage_d_support import (
    ScriptedCompletion,
    build_execution,
    execute_run,
    final_turn,
    grant,
    tool_turn,
)

CONTENT_A = "Hello, world!"
CONTENT_B = "Different content"


class ConversationRig:
    """One migrated database and the composed F1 Conversation management surface."""

    def __init__(self, engine: Engine, path: Path) -> None:
        self.engine = engine
        self.path = path
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
        self.exec_persistence = SqlAlchemyJobExecutionPersistence(engine)
        self.service = ConversationService(
            self.conv_persistence,
            self.agents,
            clock=lambda: NOW,
        )

    def stored_conversation(self, conversation_id: int) -> dict[str, object]:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text("SELECT * FROM conversations WHERE id = :id"),
                    {"id": conversation_id},
                )
                .mappings()
                .one()
            )
            return dict(row)

    def stored_turns(self, conversation_id: int) -> list[dict[str, object]]:
        with self.engine.connect() as connection:
            sql = (
                "SELECT * FROM conversation_turns WHERE conversation_id = :id ORDER BY sequence ASC"
            )
            rows = connection.execute(text(sql), {"id": conversation_id}).mappings().all()
            return [dict(r) for r in rows]

    def stored_messages(self, turn_id: int) -> list[dict[str, object]]:
        with self.engine.connect() as connection:
            query = "SELECT * FROM conversation_messages WHERE turn_id = :id ORDER BY id ASC"
            rows = connection.execute(text(query), {"id": turn_id}).mappings().all()
            return [dict(r) for r in rows]

    def stored_links(self, turn_id: int) -> list[dict[str, object]]:
        with self.engine.connect() as connection:
            query = "SELECT * FROM conversation_run_links WHERE turn_id = :id ORDER BY ordinal ASC"
            rows = connection.execute(text(query), {"id": turn_id}).mappings().all()
            return [dict(r) for r in rows]

    def add_other_agent(self) -> int:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO agent_instances(owner_user_id,agent_key,"
                    "agent_definition_version,display_name,enabled,model_provider,model_name,"
                    "created_at,updated_at) "
                    "VALUES(:owner,'nervos.chat','1','Other Agent',1,'anthropic',"
                    "'opaque/model',:now,:now)"
                ),
                {"owner": OTHER_OWNER, "now": NOW},
            )
            return int(
                connection.scalar(
                    text(
                        "SELECT id FROM agent_instances WHERE owner_user_id = :owner "
                        "ORDER BY id DESC LIMIT 1"
                    ),
                    {"owner": OTHER_OWNER},
                )
                or 0
            )


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ConversationRig]:
    built = ConversationRig(migrate(tmp_path / "nervos.db", monkeypatch), tmp_path)
    try:
        yield built
    finally:
        built.engine.dispose()


# ------------------------------------------------------------------------------------------------
# Creation & Listing
# ------------------------------------------------------------------------------------------------


def test_create_and_get_conversation(rig: ConversationRig) -> None:
    conv = rig.service.create_conversation(OWNER, AGENT, title="Test Conversation")
    assert conv.id > 0
    assert conv.owner_user_id == OWNER
    assert conv.agent_instance_id == AGENT
    assert conv.title == "Test Conversation"
    assert conv.created_at == NOW
    assert conv.updated_at == NOW

    fetched = rig.service.get_conversation(OWNER, conv.id)
    assert fetched.id == conv.id
    assert fetched.title == "Test Conversation"


def test_create_conversation_with_null_title(rig: ConversationRig) -> None:
    conv = rig.service.create_conversation(OWNER, AGENT, title=None)
    assert conv.title is None
    fetched = rig.service.get_conversation(OWNER, conv.id)
    assert fetched.title is None


def test_conversation_owner_isolation(rig: ConversationRig) -> None:
    conv = rig.service.create_conversation(OWNER, AGENT, title="Private Conv")
    other_agent = rig.add_other_agent()
    assert other_agent > 0

    with pytest.raises(ConversationNotFound):
        rig.service.get_conversation(OTHER_OWNER, conv.id)

    with pytest.raises(ConversationNotFound):
        rig.service.list_turns(OTHER_OWNER, conv.id)

    with pytest.raises(ConversationNotFound):
        rig.service.send_message(OTHER_OWNER, conv.id, "cmid-1", "Hello")

    # Empty list for other owner
    assert rig.service.list_conversations(OTHER_OWNER) == ()


def test_list_conversations_pagination(rig: ConversationRig) -> None:
    c1 = rig.service.create_conversation(OWNER, AGENT, title="First")
    c2 = rig.service.create_conversation(OWNER, AGENT, title="Second")
    c3 = rig.service.create_conversation(OWNER, AGENT, title="Third")

    all_convs = rig.service.list_conversations(OWNER, limit=10)
    assert [c.id for c in all_convs] == [c3.id, c2.id, c1.id]

    page1 = rig.service.list_conversations(OWNER, limit=2)
    assert [c.id for c in page1] == [c3.id, c2.id]

    page2 = rig.service.list_conversations(OWNER, limit=2, before_id=c2.id)
    assert [c.id for c in page2] == [c1.id]


# ------------------------------------------------------------------------------------------------
# Atomic Send & Idempotency
# ------------------------------------------------------------------------------------------------


def test_send_message_creates_turn_and_run(rig: ConversationRig) -> None:
    conv = rig.service.create_conversation(OWNER, AGENT, title="Chat")
    turn_detail, is_duplicate = rig.service.send_message(OWNER, conv.id, "client-msg-1", CONTENT_A)

    assert not is_duplicate
    assert turn_detail.turn.sequence == 1
    assert turn_detail.turn.state == TurnState.RUNNING
    assert turn_detail.turn.client_message_id == "client-msg-1"
    assert turn_detail.turn.authoritative_run_id is None
    assert turn_detail.user_message.role == MessageRole.USER
    assert turn_detail.user_message.content == CONTENT_A
    assert turn_detail.assistant_message is None
    assert turn_detail.latest_run_id is not None
    assert turn_detail.latest_run_status == "created"
    assert not turn_detail.is_retryable

    # Verify stored rows
    turns = rig.stored_turns(conv.id)
    assert len(turns) == 1
    assert turns[0]["sequence"] == 1

    msgs = rig.stored_messages(turn_detail.turn.id)
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == CONTENT_A

    links = rig.stored_links(turn_detail.turn.id)
    assert len(links) == 1
    assert links[0]["ordinal"] == 1
    assert links[0]["role"] == "initial"
    assert links[0]["run_id"] == turn_detail.latest_run_id


def test_idempotent_replay_same_id_same_content(rig: ConversationRig) -> None:
    conv = rig.service.create_conversation(OWNER, AGENT)
    turn1, is_dup1 = rig.service.send_message(OWNER, conv.id, "cmid-dup", CONTENT_A)
    assert not is_dup1

    # Same client_message_id + same content = replay!
    turn2, is_dup2 = rig.service.send_message(OWNER, conv.id, "cmid-dup", CONTENT_A)
    assert is_dup2
    assert turn2.turn.id == turn1.turn.id
    assert turn2.latest_run_id == turn1.latest_run_id

    # Verify no second turn or run was created
    assert len(rig.stored_turns(conv.id)) == 1
    assert len(rig.stored_links(turn1.turn.id)) == 1


def test_idempotent_conflict_same_id_different_content(rig: ConversationRig) -> None:
    conv = rig.service.create_conversation(OWNER, AGENT)
    rig.service.send_message(OWNER, conv.id, "cmid-conflict", CONTENT_A)

    with pytest.raises(ConversationConflict):
        rig.service.send_message(OWNER, conv.id, "cmid-conflict", CONTENT_B)


def test_one_active_turn_busy_refusal(rig: ConversationRig) -> None:
    conv = rig.service.create_conversation(OWNER, AGENT)
    rig.service.send_message(OWNER, conv.id, "cmid-1", CONTENT_A)

    # Second send with different ID while turn is active (RUNNING) raises busy
    with pytest.raises(ConversationBusy):
        rig.service.send_message(OWNER, conv.id, "cmid-2", CONTENT_B)


# ------------------------------------------------------------------------------------------------
# Terminal Projection & Finalization
# ------------------------------------------------------------------------------------------------


def test_success_projection_and_finalizer(rig: ConversationRig) -> None:
    conv = rig.service.create_conversation(OWNER, AGENT)
    detail, _ = rig.service.send_message(OWNER, conv.id, "cmid-1", CONTENT_A)
    run_id = detail.latest_run_id
    assert run_id is not None

    # Finalize projection
    projected = rig.service.project_terminal_run(
        run_id=run_id,
        status="succeeded",
        output_text="Assistant answer text",
    )
    assert projected

    # Read updated turn
    updated_detail = rig.service.list_turns(OWNER, conv.id)[0]
    assert updated_detail.turn.state == TurnState.SUCCEEDED
    assert updated_detail.turn.authoritative_run_id == run_id
    assert updated_detail.assistant_message is not None
    assert updated_detail.assistant_message.content == "Assistant answer text"
    assert updated_detail.assistant_message.source_run_id == run_id
    assert not updated_detail.is_retryable

    # Now a second turn can be created since previous turn is SUCCEEDED
    detail2, _ = rig.service.send_message(OWNER, conv.id, "cmid-2", "Turn 2 message")
    assert detail2.turn.sequence == 2


def test_failed_projection_and_retry(rig: ConversationRig) -> None:
    conv = rig.service.create_conversation(OWNER, AGENT)
    detail, _ = rig.service.send_message(OWNER, conv.id, "cmid-1", CONTENT_A)
    run_id = detail.latest_run_id
    assert run_id is not None

    # Project failure
    rig.service.project_terminal_run(
        run_id=run_id,
        status="failed",
        error_code="model_error",
    )

    failed_turn = rig.service.list_turns(OWNER, conv.id)[0]
    assert failed_turn.turn.state == TurnState.FAILED
    assert failed_turn.assistant_message is None
    assert failed_turn.is_retryable

    # Manual retry
    retry_detail, is_dup = rig.service.retry_turn(OWNER, conv.id, failed_turn.turn.id)
    assert not is_dup
    assert retry_detail.turn.state == TurnState.RUNNING
    assert retry_detail.latest_run_id != run_id

    # Verify run links: ordinal 1 (initial) and ordinal 2 (retry)
    links = rig.stored_links(failed_turn.turn.id)
    assert len(links) == 2
    assert links[0]["ordinal"] == 1
    assert links[0]["role"] == "initial"
    assert links[1]["ordinal"] == 2
    assert links[1]["role"] == "retry"
    assert links[1]["run_id"] == retry_detail.latest_run_id

    # Replay of retry while running returns same active retry turn
    replay_retry, is_dup_retry = rig.service.retry_turn(OWNER, conv.id, failed_turn.turn.id)
    assert is_dup_retry
    assert replay_retry.latest_run_id == retry_detail.latest_run_id

    # Now finalize retry run with success
    retry_run_id = retry_detail.latest_run_id
    assert retry_run_id is not None
    rig.service.project_terminal_run(
        run_id=retry_run_id,
        status="succeeded",
        output_text="Retry success output",
    )

    succeeded_turn = rig.service.list_turns(OWNER, conv.id)[0]
    assert succeeded_turn.turn.state == TurnState.SUCCEEDED
    assert succeeded_turn.turn.authoritative_run_id == retry_run_id
    assert succeeded_turn.assistant_message is not None
    assert succeeded_turn.assistant_message.content == "Retry success output"
    assert not succeeded_turn.is_retryable


def test_cannot_retry_succeeded_or_older_turn(rig: ConversationRig) -> None:
    conv = rig.service.create_conversation(OWNER, AGENT)
    d1, _ = rig.service.send_message(OWNER, conv.id, "cmid-1", CONTENT_A)
    assert d1.latest_run_id is not None

    rig.service.project_terminal_run(
        run_id=d1.latest_run_id,
        status="succeeded",
        output_text="Answer 1",
    )

    # Succeeded turn cannot be retried
    with pytest.raises(TurnNotRetryable):
        rig.service.retry_turn(OWNER, conv.id, d1.turn.id)

    # Send turn 2
    d2, _ = rig.service.send_message(OWNER, conv.id, "cmid-2", "Turn 2")
    assert d2.latest_run_id is not None
    rig.service.project_terminal_run(
        run_id=d2.latest_run_id,
        status="failed",
        error_code="model_error",
    )

    # Turn 2 is latest and failed: it can be retried
    d2_retry, _ = rig.service.retry_turn(OWNER, conv.id, d2.turn.id)
    assert d2_retry.turn.state == TurnState.RUNNING


def test_crash_recovery_reconciliation(rig: ConversationRig) -> None:
    conv = rig.service.create_conversation(OWNER, AGENT)
    detail, _ = rig.service.send_message(OWNER, conv.id, "cmid-1", CONTENT_A)
    run_id = detail.latest_run_id
    assert run_id is not None

    # Simulate: run status was updated to SUCCEEDED with output_text directly in runs table
    # (e.g., worker terminalized the run, but crashed before assistant finalizer)
    with rig.engine.begin() as connection:
        update_sql = (
            "UPDATE runs SET status = 'succeeded', output_text = 'Recovered output', "
            "started_at = '2026-09-15 12:00:00', finished_at = '2026-09-15 12:00:00', "
            "elapsed_ms = 100 WHERE id = :run_id"
        )
        connection.execute(text(update_sql), {"run_id": run_id})

    # Turn is still RUNNING
    assert rig.service.list_turns(OWNER, conv.id)[0].turn.state == TurnState.RUNNING

    # Run reconciliation pass
    reconciled_count = rig.service.reconcile_unprojected_terminal_runs()
    assert reconciled_count == 1

    # Turn is now SUCCEEDED with ASSISTANT message
    turn = rig.service.list_turns(OWNER, conv.id)[0]
    assert turn.turn.state == TurnState.SUCCEEDED
    assert turn.turn.authoritative_run_id == run_id
    assert turn.assistant_message is not None
    assert turn.assistant_message.content == "Recovered output"


def test_stage_e_runs_remain_conversationless(rig: ConversationRig) -> None:
    """Direct submit_run from AgentService/Trigger creates no conversation records."""
    run = rig.agents.submit_run(OWNER, AGENT, "Direct chat run")
    assert run.id > 0

    # Verify no conversation, turn, or link was created
    assert len(rig.service.list_conversations(OWNER)) == 0

    with rig.engine.connect() as connection:
        links_count = connection.scalar(text("SELECT count(*) FROM conversation_run_links"))
        turns_count = connection.scalar(text("SELECT count(*) FROM conversation_turns"))
        assert links_count == 0
        assert turns_count == 0


def test_same_turn_run_integrity_enforced(rig: ConversationRig) -> None:
    """A Run linked to Turn 1 cannot be projected as assistant source or authority for Turn 2."""
    conv = rig.service.create_conversation(OWNER, AGENT)
    t1_detail, _ = rig.service.send_message(OWNER, conv.id, "cmid-t1", "Turn 1")
    t1_run_id = t1_detail.latest_run_id
    assert t1_run_id is not None

    rig.service.project_terminal_run(run_id=t1_run_id, status="succeeded", output_text="T1 answer")

    t2_detail, _ = rig.service.send_message(OWNER, conv.id, "cmid-t2", "Turn 2")
    t2_id = t2_detail.turn.id

    # Attempting to insert an assistant message for Turn 2 with Turn 1's Run must fail composite FK
    with pytest.raises(IntegrityError), rig.engine.begin() as connection:
        insert_sql = (
            "INSERT INTO conversation_messages(turn_id, role, content, source_run_id, created_at) "
            "VALUES(:turn_id, 'assistant', 'Invalid cross-turn answer', :run_id, :now)"
        )
        connection.execute(
            text(insert_sql),
            {"turn_id": t2_id, "run_id": t1_run_id, "now": NOW},
        )


def test_cancelled_and_ambiguous_projection(rig: ConversationRig) -> None:
    """Test CANCELLED and AMBIGUOUS terminal projection paths."""
    conv = rig.service.create_conversation(OWNER, AGENT)
    t1, _ = rig.service.send_message(OWNER, conv.id, "cmid-c1", "Msg 1")
    assert t1.latest_run_id is not None

    # Project cancelled
    rig.service.project_terminal_run(run_id=t1.latest_run_id, status="cancelled")
    t1_detail = rig.service.list_turns(OWNER, conv.id)[0]
    assert t1_detail.turn.state == TurnState.CANCELLED
    assert t1_detail.assistant_message is None
    assert t1_detail.is_retryable

    # Cancelled turn can be retried
    t1_retried, _ = rig.service.retry_turn(OWNER, conv.id, t1.turn.id)
    assert t1_retried.turn.state == TurnState.RUNNING
    retry_run_id = t1_retried.latest_run_id
    assert retry_run_id is not None

    # Project ambiguous
    rig.service.project_terminal_run(
        run_id=retry_run_id,
        status="failed",
        error_code=EXECUTION_OUTCOME_AMBIGUOUS,
    )
    t1_ambiguous = rig.service.list_turns(OWNER, conv.id)[0]
    assert t1_ambiguous.turn.state == TurnState.AMBIGUOUS
    assert t1_ambiguous.assistant_message is None
    assert not t1_ambiguous.is_retryable

    # AMBIGUOUS turn cannot be retried
    with pytest.raises(TurnNotRetryable):
        rig.service.retry_turn(OWNER, conv.id, t1.turn.id)


def test_two_successful_runs_cas_race(rig: ConversationRig) -> None:
    """When two runs linked to same turn succeed, CAS admits only one as authoritative."""
    conv = rig.service.create_conversation(OWNER, AGENT)
    detail, _ = rig.service.send_message(OWNER, conv.id, "cmid-race", "Race test")
    run_1 = detail.latest_run_id
    assert run_1 is not None

    # Mark run 1 as failed to allow retry
    rig.service.project_terminal_run(run_id=run_1, status="failed", error_code="err")
    retry_detail, _ = rig.service.retry_turn(OWNER, conv.id, detail.turn.id)
    run_2 = retry_detail.latest_run_id
    assert run_2 is not None

    # Now both runs appear successful
    with rig.engine.begin() as connection:
        run_update = (
            "UPDATE runs SET status = 'succeeded', output_text = :out, "
            "started_at = '2026-09-15 12:00:00', finished_at = '2026-09-15 12:00:00', "
            "elapsed_ms = 10 WHERE id = :id"
        )
        connection.execute(text(run_update), {"id": run_1, "out": "Run 1 output"})
        connection.execute(text(run_update), {"id": run_2, "out": "Run 2 output"})

    # First finalizer call for run_1 succeeds and CAS sets authoritative_run_id
    p1 = rig.service.project_terminal_run(
        run_id=run_1, status="succeeded", output_text="Run 1 output"
    )
    assert p1

    # Second finalizer call for run_2 is safe no-op (CAS fails because turn already succeeded)
    p2 = rig.service.project_terminal_run(
        run_id=run_2, status="succeeded", output_text="Run 2 output"
    )
    assert not p2

    # Verify turn has exactly 1 assistant message from Run 1
    t = rig.service.list_turns(OWNER, conv.id)[0]
    assert t.turn.state == TurnState.SUCCEEDED
    assert t.turn.authoritative_run_id == run_1
    assert t.assistant_message is not None
    assert t.assistant_message.content == "Run 1 output"
    assert len(rig.stored_messages(t.turn.id)) == 2  # 1 user + 1 assistant


def test_tool_enabled_conversation_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool-enabled conversational Run executes through the normal Worker/ToolLoop."""
    engine = migrate(tmp_path / "tool_conv.db", monkeypatch)
    definitions = SqlAlchemyToolDefinitionPersistence(engine)
    descriptors = reconcile_builtin_definitions(
        definitions, specs=builtin_tool_specs(clock=lambda: NOW), now=NOW
    )
    d_rig = DRig(engine=engine, descriptors=tuple(descriptors), instance_id=AGENT)
    current_time = next(d for d in descriptors if d.upstream_name == "current_time")
    grant(d_rig, descriptor=current_time)

    conv_persistence = SqlAlchemyConversationPersistence(engine, sleep=lambda _: None)
    from nervos_core.application.agent_definitions import create_builtin_definition_registry

    known_providers = ModelProviderCatalog([], known=("anthropic", "openai"))
    agents = AgentService(
        SqlAlchemyAgentPersistence(create_session_factory(engine)),
        create_builtin_definition_registry(),
        lambda: NOW,
        known_providers,
        SqlAlchemyJobPersistence(engine),
    )
    conv_service = ConversationService(conv_persistence, agents, clock=lambda: NOW)

    # Create conversation and send tool-requesting message
    conv = conv_service.create_conversation(OWNER, AGENT, "Tool Conversation")
    detail, _ = conv_service.send_message(OWNER, conv.id, "cmid-tool-1", "What time is it?")
    run_id = detail.latest_run_id
    assert run_id is not None

    completion = ScriptedCompletion(
        tool_turn(current_time.model_name, '{"timezone":"UTC"}', call_id="call-1"),
        final_turn("The time is known precisely."),
    )
    stage = build_execution(d_rig, completion=completion)
    outcome = asyncio.run(execute_run(stage, run_id))
    assert outcome is not None
    assert outcome.status == "succeeded"
    assert outcome.output_text == "The time is known precisely."

    # Finalize conversation projection
    conv_service.project_terminal_run(
        run_id=run_id, status="succeeded", output_text=outcome.output_text
    )

    t = conv_service.list_turns(OWNER, conv.id)[0]
    assert t.turn.state == TurnState.SUCCEEDED
    assert t.assistant_message is not None
    assert t.assistant_message.content == "The time is known precisely."
    # Conversation messages contains ONLY user and assistant, not raw tool calls
    msgs = conv_service.list_turns(OWNER, conv.id)[0]
    assert msgs.user_message.role == MessageRole.USER
    assert msgs.assistant_message is not None
    assert msgs.assistant_message.role == MessageRole.ASSISTANT


def test_submission_transaction_rollback_on_capacity_exceeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If queue capacity is exceeded during submission, all conversation rows roll back."""
    engine = migrate(tmp_path / "rollback.db", monkeypatch)
    session_factory = create_session_factory(engine)
    from nervos_core.application.agent_definitions import create_builtin_definition_registry
    from nervos_core.application.errors import QueueCapacityExceeded

    known_providers = ModelProviderCatalog([], known=("anthropic", "openai"))
    agents = AgentService(
        SqlAlchemyAgentPersistence(session_factory),
        create_builtin_definition_registry(),
        lambda: NOW,
        known_providers,
        SqlAlchemyJobPersistence(engine),
    )
    # Set capacity = 1 so second submission fails
    conv_persistence = SqlAlchemyConversationPersistence(
        engine, max_pending=1, sleep=lambda _: None
    )
    conv_service = ConversationService(conv_persistence, agents, clock=lambda: NOW)

    conv1 = conv_service.create_conversation(OWNER, AGENT, "Conv 1")
    conv2 = conv_service.create_conversation(OWNER, AGENT, "Conv 2")

    # First send occupies the 1 pending job slot
    conv_service.send_message(OWNER, conv1.id, "cmid-1", "Msg 1")

    # Second send raises QueueCapacityExceeded during insert_run_and_job_on_connection
    with pytest.raises(QueueCapacityExceeded):
        conv_service.send_message(OWNER, conv2.id, "cmid-fail", "Should roll back")

    # Verify conv2 has zero turns, zero messages, zero links committed
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM conversation_turns WHERE conversation_id = :cid"),
                {"cid": conv2.id},
            )
            == 0
        )
        assert connection.scalar(text("SELECT count(*) FROM conversation_turns")) == 1
        assert connection.scalar(text("SELECT count(*) FROM conversation_messages")) == 1
        assert connection.scalar(text("SELECT count(*) FROM conversation_run_links")) == 1
        assert connection.scalar(text("SELECT count(*) FROM runs")) == 1
        assert connection.scalar(text("SELECT count(*) FROM jobs")) == 1


def test_conversation_archive_and_unarchive_lifecycle(rig: ConversationRig) -> None:
    from nervos_core.application.conversations import ConversationArchived
    from nervos_core.domain.conversations import ConversationStatus

    conv = rig.service.create_conversation(OWNER, AGENT, "Archivable Conversation")
    assert conv.status == ConversationStatus.ACTIVE
    assert conv.archived_at is None

    # Send a message to verify normal operation
    rig.service.send_message(OWNER, conv.id, "cmid-arch-1", "Hello before archive")

    # Archive the conversation
    archived = rig.service.archive_conversation(OWNER, conv.id)
    assert archived.status == ConversationStatus.ARCHIVED
    assert archived.archived_at is not None

    # Default active list excludes archived conversation
    active_list = rig.service.list_conversations(OWNER, status="active")
    assert not any(c.id == conv.id for c in active_list)

    # Archived list includes it
    archived_list = rig.service.list_conversations(OWNER, status="archived")
    assert any(c.id == conv.id for c in archived_list)

    # Detail remains readable
    detail = rig.service.get_conversation(OWNER, conv.id)
    assert detail.status == ConversationStatus.ARCHIVED

    # Sending a message to an archived conversation is rejected
    with pytest.raises(ConversationArchived):
        rig.service.send_message(OWNER, conv.id, "cmid-arch-2", "Should be rejected")

    # Unarchive the conversation
    unarchived = rig.service.unarchive_conversation(OWNER, conv.id)
    assert unarchived.status == ConversationStatus.ACTIVE
    assert unarchived.archived_at is None

    # Appears back in active list
    active_list_after = rig.service.list_conversations(OWNER, status="active")
    assert any(c.id == conv.id for c in active_list_after)


def test_conversation_delete_lifecycle(rig: ConversationRig) -> None:
    conv = rig.service.create_conversation(OWNER, AGENT, "Deletable Conversation")
    rig.service.send_message(OWNER, conv.id, "cmid-del-1", "Hello before delete")

    # Delete the conversation
    rig.service.delete_conversation(OWNER, conv.id)

    # Excluded from active list
    active_list = rig.service.list_conversations(OWNER, status="active")
    assert not any(c.id == conv.id for c in active_list)

    # Excluded from archived list
    archived_list = rig.service.list_conversations(OWNER, status="archived")
    assert not any(c.id == conv.id for c in archived_list)

    # Detail returns ConversationNotFound
    with pytest.raises(ConversationNotFound):
        rig.service.get_conversation(OWNER, conv.id)

    # List turns returns ConversationNotFound
    with pytest.raises(ConversationNotFound):
        rig.service.list_turns(OWNER, conv.id)

    # Send message returns ConversationNotFound
    with pytest.raises(ConversationNotFound):
        rig.service.send_message(OWNER, conv.id, "cmid-del-2", "Should fail on deleted")


def test_conversation_delete_during_active_run(rig: ConversationRig) -> None:
    conv = rig.service.create_conversation(OWNER, AGENT, "Active Run Conv")
    t_detail, _ = rig.service.send_message(OWNER, conv.id, "cmid-inflight", "In flight query")
    run_id = t_detail.latest_run_id
    assert run_id is not None

    # Delete conversation while run is in-flight
    rig.service.delete_conversation(OWNER, conv.id)

    # Execution row (Run) still exists and is not cascaded
    with rig.engine.connect() as connection:
        run_count = connection.scalar(
            text("SELECT count(*) FROM runs WHERE id = :id"), {"id": run_id}
        )
        assert run_count == 1
        snapshot_count = connection.scalar(
            text("SELECT count(*) FROM run_context_snapshots WHERE run_id = :id"), {"id": run_id}
        )
        assert snapshot_count == 1

    # Finalizer projects the run successfully without un-deleting or resurrecting the conversation
    projected = rig.service.project_terminal_run(
        run_id=run_id, status="succeeded", output_text="Finished answer"
    )
    assert projected

    with rig.engine.connect() as connection:
        conv_status = connection.scalar(
            text("SELECT status FROM conversations WHERE id = :id"), {"id": conv.id}
        )
        assert conv_status == "deleted"
