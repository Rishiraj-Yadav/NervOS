"""Unit tests for F2 ContextBuilder and canonical rendering."""

from datetime import UTC, datetime

from nervos_core.application.context_builder import ContextBuilder
from nervos_core.application.memory import RetrievedMemoryItem
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.context import (
    AGENT_MEMORY_HEADER,
    COMPACTION_HEADER_STORED,
    COMPACTION_WRAPPER_PREFIX,
    USER_MEMORY_HEADER,
    CompactionData,
    HistoricalMessage,
    SelectedMemory,
    compute_context_digest,
    deserialize_history_messages,
    deserialize_id_list,
    deserialize_selected_memories,
    format_stored_compaction_v1,
    render_context_v1,
    render_context_v2,
    serialize_history_messages,
    serialize_id_list,
    serialize_selected_memories,
)
from nervos_core.domain.conversations import (
    ConversationMessage,
    MessageRole,
)
from nervos_core.domain.memory import MemoryProvenanceType, MemoryScope


def _msg(turn_id: int, msg_id: int, role: MessageRole, content: str) -> ConversationMessage:
    return ConversationMessage(
        id=msg_id,
        turn_id=turn_id,
        role=role,
        content=content,
        source_run_id=None if role == MessageRole.USER else 100 + turn_id,
        created_at=datetime.now(UTC),
    )


def test_render_context_v1_empty_history() -> None:
    rendered = render_context_v1(current_user_text="Hello world")
    assert rendered == "Hello world"


def test_render_context_v1_with_history() -> None:
    history = (
        HistoricalMessage(1, 1, 10, MessageRole.USER, "What is SQLite?"),
        HistoricalMessage(1, 1, 11, MessageRole.ASSISTANT, "SQLite is a C-language library."),
    )
    rendered = render_context_v1(current_user_text="How fast is it?", history_messages=history)
    expected = (
        "Turn 1 (User): What is SQLite?\n\n"
        "Turn 1 (Assistant): SQLite is a C-language library.\n\n"
        "How fast is it?"
    )
    assert rendered == expected


def test_render_context_v1_with_compaction_and_history() -> None:
    history = (
        HistoricalMessage(2, 2, 20, MessageRole.USER, "Tell me more."),
        HistoricalMessage(2, 2, 21, MessageRole.ASSISTANT, "It is embedded."),
    )
    compaction = "Turn 1:\nUser: Initial query\nAssistant: Initial answer"
    rendered = render_context_v1(
        current_user_text="Followup",
        history_messages=history,
        injected_compaction_text=compaction,
    )
    expected = (
        f"{COMPACTION_WRAPPER_PREFIX}{compaction}\n\n"
        "Turn 2 (User): Tell me more.\n\n"
        "Turn 2 (Assistant): It is embedded.\n\n"
        "Followup"
    )
    assert rendered == expected


def test_format_stored_compaction_v1() -> None:
    turns = [
        (1, "Hello", "Hi there"),
        (2, "What is 2+2?", "4"),
    ]
    stored = format_stored_compaction_v1(turns)
    expected = (
        f"{COMPACTION_HEADER_STORED}\n"
        "Turn 1:\nUser: Hello\nAssistant: Hi there\n\n"
        "Turn 2:\nUser: What is 2+2?\nAssistant: 4"
    )
    assert stored == expected


def test_context_builder_empty_history() -> None:
    now = datetime.now(UTC)
    def_id = AgentDefinitionId("nervos.chat", "1")
    snapshot = ContextBuilder.assemble(
        current_user_text="New query",
        candidate_turns=[],
        current_compaction=None,
        agent_definition_id=def_id,
        now=now,
    )
    assert snapshot.current_user_text == "New query"
    assert snapshot.rendered_context == "New query"
    assert snapshot.history_messages == ()
    assert snapshot.injected_compaction_text is None
    assert snapshot.content_digest == compute_context_digest("New query")
    assert snapshot.actual_total_bytes == len(b"New query")
    assert snapshot.actual_total_code_points == len("New query")


def test_context_builder_contiguous_suffix() -> None:
    now = datetime.now(UTC)
    def_id = AgentDefinitionId("nervos.chat", "1")
    candidate_turns = [
        (
            1,
            _msg(1, 10, MessageRole.USER, "Turn 1 question"),
            _msg(1, 11, MessageRole.ASSISTANT, "Turn 1 answer"),
        ),
        (
            2,
            _msg(2, 20, MessageRole.USER, "Turn 2 question"),
            _msg(2, 21, MessageRole.ASSISTANT, "Turn 2 answer"),
        ),
        (
            3,
            _msg(3, 30, MessageRole.USER, "Turn 3 question"),
            _msg(3, 31, MessageRole.ASSISTANT, "Turn 3 answer"),
        ),
    ]
    snapshot = ContextBuilder.assemble(
        current_user_text="Turn 4 question",
        candidate_turns=candidate_turns,
        current_compaction=None,
        agent_definition_id=def_id,
        now=now,
    )
    assert len(snapshot.history_messages) == 6
    assert [m.sequence for m in snapshot.history_messages] == [1, 1, 2, 2, 3, 3]
    assert snapshot.selected_turn_ids == (1, 2, 3)
    assert snapshot.selected_message_ids == (10, 11, 20, 21, 30, 31)


def test_context_builder_stopping_at_oversized_pair() -> None:
    now = datetime.now(UTC)
    def_id = AgentDefinitionId("nervos.chat", "1")
    # Turn 1 is small, Turn 2 is massive, Turn 3 is small
    candidate_turns = [
        (
            1,
            _msg(1, 10, MessageRole.USER, "Small 1"),
            _msg(1, 11, MessageRole.ASSISTANT, "Small answer 1"),
        ),
        (
            2,
            _msg(2, 20, MessageRole.USER, "Big " * 1000),
            _msg(2, 21, MessageRole.ASSISTANT, "Big answer " * 1000),
        ),
        (
            3,
            _msg(3, 30, MessageRole.USER, "Small 3"),
            _msg(3, 31, MessageRole.ASSISTANT, "Small answer 3"),
        ),
    ]
    # Total budget 2000 bytes
    snapshot = ContextBuilder.assemble(
        current_user_text="Current query",
        candidate_turns=candidate_turns,
        current_compaction=None,
        agent_definition_id=def_id,
        now=now,
        max_bytes=2000,
        max_code_points=1000,
    )
    # Turn 3 fits, Turn 2 does not fit -> stop immediately! Turn 1 must NOT be included.
    assert snapshot.selected_turn_ids == (3,)
    assert [m.sequence for m in snapshot.history_messages] == [3, 3]


def test_context_builder_with_valid_compaction() -> None:
    now = datetime.now(UTC)
    def_id = AgentDefinitionId("nervos.chat", "1")
    # 11 turns: Turns 1..11. Recent window can hold at most 10 turns (turns 2..11).
    candidate_turns = [
        (
            i,
            _msg(i, i * 10, MessageRole.USER, f"T{i} U"),
            _msg(i, i * 10 + 1, MessageRole.ASSISTANT, f"T{i} A"),
        )
        for i in range(1, 12)
    ]
    # Compaction covers Turn 1
    stored_comp = format_stored_compaction_v1([(1, "T1 U", "T1 A")])
    comp_data = CompactionData(
        version=1,
        source_start_sequence=1,
        source_end_sequence=1,
        content=stored_comp,
        content_digest=compute_context_digest(stored_comp),
    )
    snapshot = ContextBuilder.assemble(
        current_user_text="T12 U",
        candidate_turns=candidate_turns,
        current_compaction=comp_data,
        agent_definition_id=def_id,
        now=now,
    )
    # Recent history selects turns 2..11 (10 pairs). Compaction covers Turn 1.
    assert snapshot.compaction_version == 1
    assert snapshot.injected_compaction_text == "Turn 1:\nUser: T1 U\nAssistant: T1 A"
    assert snapshot.selected_turn_ids == tuple(range(2, 12))


def test_context_builder_stale_compaction_gap_rejected() -> None:
    now = datetime.now(UTC)
    def_id = AgentDefinitionId("nervos.chat", "1")
    # All candidate succeeded turns are 1, 2, 3, 4
    candidate_turns = [
        (1, _msg(1, 10, MessageRole.USER, "T1"), _msg(1, 11, MessageRole.ASSISTANT, "T1 A")),
        (2, _msg(2, 20, MessageRole.USER, "T2"), _msg(2, 21, MessageRole.ASSISTANT, "T2 A")),
        (3, _msg(3, 30, MessageRole.USER, "T3"), _msg(3, 31, MessageRole.ASSISTANT, "T3 A")),
        (4, _msg(4, 40, MessageRole.USER, "T4"), _msg(4, 41, MessageRole.ASSISTANT, "T4 A")),
    ]
    # Compaction only covers Turn 1 (end_seq = 1)
    stored_comp = format_stored_compaction_v1([(1, "T1", "T1 A")])
    comp_data = CompactionData(
        version=1,
        source_start_sequence=1,
        source_end_sequence=1,
        content=stored_comp,
        content_digest=compute_context_digest(stored_comp),
    )
    # Suppose recent history only selects Turns 3 and 4 (oldest selected = 3).
    # Expected compaction end is 2 (Turn 2 sits in the gap!). But compaction end is 1.
    # -> Compaction is STALE and must be rejected!
    snapshot = ContextBuilder.assemble(
        current_user_text="T5",
        candidate_turns=candidate_turns,
        current_compaction=comp_data,
        agent_definition_id=def_id,
        now=now,
        max_bytes=300,  # only fits turns 3 & 4
    )
    assert snapshot.injected_compaction_text is None
    assert snapshot.compaction_version is None


def test_serialization_helpers() -> None:
    history = (
        HistoricalMessage(1, 1, 10, MessageRole.USER, "Hello"),
        HistoricalMessage(1, 1, 11, MessageRole.ASSISTANT, "World"),
    )
    raw_json = serialize_history_messages(history)
    deserialized = deserialize_history_messages(raw_json)
    assert deserialized == history

    ids = (1, 2, 3, 4)
    raw_ids = serialize_id_list(ids)
    deserialized_ids = deserialize_id_list(raw_ids)
    assert deserialized_ids == ids

    memories = (
        SelectedMemory(1, 1, "user", "user_authored", "Prefers Python", b"x" * 32),
        SelectedMemory(2, 1, "agent", "user_approved_inferred", "Uses SQLite", b"y" * 32),
    )
    mem_json = serialize_selected_memories(memories)
    deserialized_mem = deserialize_selected_memories(mem_json)
    assert deserialized_mem == memories


def test_render_context_v2_with_memory_and_history() -> None:
    history = (
        HistoricalMessage(1, 1, 10, MessageRole.USER, "Prior question"),
        HistoricalMessage(1, 1, 11, MessageRole.ASSISTANT, "Prior answer"),
    )
    rendered = render_context_v2(
        current_user_text="Current question",
        injected_user_memory_text=f"{USER_MEMORY_HEADER}\n- User fact",
        injected_agent_memory_text=f"{AGENT_MEMORY_HEADER}\n- Agent fact",
        injected_compaction_text="Compacted turn",
        history_messages=history,
    )
    expected = (
        f"{USER_MEMORY_HEADER}\n- User fact\n\n"
        f"{AGENT_MEMORY_HEADER}\n- Agent fact\n\n"
        f"{COMPACTION_WRAPPER_PREFIX}Compacted turn\n\n"
        "Turn 1 (User): Prior question\n\n"
        "Turn 1 (Assistant): Prior answer\n\n"
        "Current question"
    )
    assert rendered == expected


def test_context_builder_with_memory_candidates() -> None:
    now = datetime.now(UTC)
    def_id = AgentDefinitionId("nervos.chat", "1")
    candidates = (
        RetrievedMemoryItem(
            1,
            1,
            MemoryScope.USER,
            MemoryProvenanceType.USER_AUTHORED,
            "User prefers Python",
            b"u" * 32,
            now,
        ),
        RetrievedMemoryItem(
            2,
            1,
            MemoryScope.AGENT,
            MemoryProvenanceType.USER_AUTHORED,
            "Agent runs on Linux",
            b"a" * 32,
            now,
        ),
    )
    snapshot = ContextBuilder.assemble(
        current_user_text="Hello",
        candidate_turns=[],
        current_compaction=None,
        agent_definition_id=def_id,
        now=now,
        memory_candidates=candidates,
    )
    assert snapshot.builder_version == "nervos.context.v2"
    assert snapshot.schema_version == 2
    assert snapshot.injected_user_memory_text == f"{USER_MEMORY_HEADER}\n- User prefers Python"
    assert snapshot.injected_agent_memory_text == f"{AGENT_MEMORY_HEADER}\n- Agent runs on Linux"
    assert len(snapshot.selected_memories) == 2
    assert snapshot.selected_memories[0].content == "User prefers Python"
    assert snapshot.selected_memories[1].content == "Agent runs on Linux"
