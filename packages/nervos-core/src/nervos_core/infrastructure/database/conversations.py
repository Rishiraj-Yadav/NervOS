"""Owner-scoped Conversation, Turn, Message, and RunLink persistence."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import Engine, and_, func, insert, select, update
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import IntegrityError

from nervos_core.application.context_builder import ContextBuilder
from nervos_core.application.conversations import (
    ConversationBusy,
    ConversationConflict,
    ConversationNotFound,
    ConversationTurnDetail,
    TurnNotFound,
    TurnNotRetryable,
)
from nervos_core.application.model_completion import EXECUTION_OUTCOME_AMBIGUOUS
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.context import (
    COMPACTION_HEADER_STORED,
    CompactionData,
    ContextSnapshotData,
    compute_context_digest,
    deserialize_history_messages,
    deserialize_id_list,
    deserialize_selected_memories,
    serialize_history_messages,
    serialize_id_list,
    serialize_selected_memories,
)
from nervos_core.domain.conversations import (
    Conversation,
    ConversationMessage,
    ConversationTurn,
    MessageRole,
    RunLinkRole,
    TurnState,
)
from nervos_core.domain.runs import RunLimits, RunStatus
from nervos_core.infrastructure.database.jobs import (
    insert_run_and_job_on_connection,
)
from nervos_core.infrastructure.database.memory import (
    _retrieve_memory_candidates_on_connection,
)
from nervos_core.infrastructure.database.models import (
    AgentInstanceRecord,
    ConversationCompactionRecord,
    ConversationMessageRecord,
    ConversationRecord,
    ConversationRunLinkRecord,
    ConversationTurnRecord,
    RunContextSnapshotRecord,
    RunRecord,
)
from nervos_core.infrastructure.database.transaction import TransactionRunner


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _rowcount(result: object) -> int:
    return int(getattr(result, "rowcount", 0))


def _to_conversation(record: ConversationRecord | RowMapping) -> Conversation:
    created = _as_utc(record["created_at"] if isinstance(record, RowMapping) else record.created_at)
    updated = _as_utc(record["updated_at"] if isinstance(record, RowMapping) else record.updated_at)
    assert created is not None
    assert updated is not None
    title = record["title"] if isinstance(record, RowMapping) else record.title
    return Conversation(
        id=int(record["id"] if isinstance(record, RowMapping) else record.id),
        owner_user_id=int(
            record["owner_user_id"] if isinstance(record, RowMapping) else record.owner_user_id
        ),
        agent_instance_id=int(
            record["agent_instance_id"]
            if isinstance(record, RowMapping)
            else record.agent_instance_id
        ),
        title=str(title) if title is not None else None,
        created_at=created,
        updated_at=updated,
    )


def _to_turn(record: ConversationTurnRecord | RowMapping) -> ConversationTurn:
    created = _as_utc(record["created_at"] if isinstance(record, RowMapping) else record.created_at)
    started = _as_utc(record["started_at"] if isinstance(record, RowMapping) else record.started_at)
    finished = _as_utc(
        record["finished_at"] if isinstance(record, RowMapping) else record.finished_at
    )
    assert created is not None
    auth_run_id = (
        record["authoritative_run_id"]
        if isinstance(record, RowMapping)
        else record.authoritative_run_id
    )
    return ConversationTurn(
        id=int(record["id"] if isinstance(record, RowMapping) else record.id),
        conversation_id=int(
            record["conversation_id"] if isinstance(record, RowMapping) else record.conversation_id
        ),
        sequence=int(record["sequence"] if isinstance(record, RowMapping) else record.sequence),
        state=TurnState(str(record["state"] if isinstance(record, RowMapping) else record.state)),
        client_message_id=str(
            record["client_message_id"]
            if isinstance(record, RowMapping)
            else record.client_message_id
        ),
        content_digest=bytes(
            record["content_digest"] if isinstance(record, RowMapping) else record.content_digest
        ),
        authoritative_run_id=int(auth_run_id) if auth_run_id is not None else None,
        created_at=created,
        started_at=started,
        finished_at=finished,
    )


def _to_message(record: ConversationMessageRecord | RowMapping) -> ConversationMessage:
    created = _as_utc(record["created_at"] if isinstance(record, RowMapping) else record.created_at)
    assert created is not None
    src_run = record["source_run_id"] if isinstance(record, RowMapping) else record.source_run_id
    return ConversationMessage(
        id=int(record["id"] if isinstance(record, RowMapping) else record.id),
        turn_id=int(record["turn_id"] if isinstance(record, RowMapping) else record.turn_id),
        role=MessageRole(str(record["role"] if isinstance(record, RowMapping) else record.role)),
        content=str(record["content"] if isinstance(record, RowMapping) else record.content),
        source_run_id=int(src_run) if src_run is not None else None,
        created_at=created,
    )


def _to_context_snapshot(record: RunContextSnapshotRecord | RowMapping) -> ContextSnapshotData:
    created = _as_utc(record["created_at"] if isinstance(record, RowMapping) else record.created_at)
    assert created is not None
    comp_ver = (
        record["compaction_version"]
        if isinstance(record, RowMapping)
        else record.compaction_version
    )
    comp_start = (
        record["compaction_source_start"]
        if isinstance(record, RowMapping)
        else record.compaction_source_start
    )
    comp_end = (
        record["compaction_source_end"]
        if isinstance(record, RowMapping)
        else record.compaction_source_end
    )
    inj_comp = (
        record["injected_compaction_text"]
        if isinstance(record, RowMapping)
        else record.injected_compaction_text
    )
    hist_raw = (
        record["history_messages"] if isinstance(record, RowMapping) else record.history_messages
    )
    turn_ids_raw = (
        record["selected_turn_ids"] if isinstance(record, RowMapping) else record.selected_turn_ids
    )
    msg_ids_raw = (
        record["selected_message_ids"]
        if isinstance(record, RowMapping)
        else record.selected_message_ids
    )
    mem_raw = (
        record["memory_items_json"]
        if isinstance(record, RowMapping)
        else getattr(record, "memory_items_json", "[]")
    )
    inj_user_mem = (
        record["injected_user_memory_text"]
        if isinstance(record, RowMapping)
        else getattr(record, "injected_user_memory_text", None)
    )
    inj_agent_mem = (
        record["injected_agent_memory_text"]
        if isinstance(record, RowMapping)
        else getattr(record, "injected_agent_memory_text", None)
    )
    return ContextSnapshotData(
        run_id=int(record["run_id"] if isinstance(record, RowMapping) else record.run_id),
        turn_id=int(record["turn_id"] if isinstance(record, RowMapping) else record.turn_id),
        schema_version=int(
            record["schema_version"] if isinstance(record, RowMapping) else record.schema_version
        ),
        builder_version=str(
            record["builder_version"] if isinstance(record, RowMapping) else record.builder_version
        ),
        current_user_text=str(
            record["current_user_text"]
            if isinstance(record, RowMapping)
            else record.current_user_text
        ),
        history_messages=deserialize_history_messages(str(hist_raw)),
        selected_turn_ids=deserialize_id_list(str(turn_ids_raw)),
        selected_message_ids=deserialize_id_list(str(msg_ids_raw)),
        compaction_version=int(comp_ver) if comp_ver is not None else None,
        compaction_source_start=int(comp_start) if comp_start is not None else None,
        compaction_source_end=int(comp_end) if comp_end is not None else None,
        injected_compaction_text=str(inj_comp) if inj_comp is not None else None,
        agent_key=str(record["agent_key"] if isinstance(record, RowMapping) else record.agent_key),
        agent_definition_version=str(
            record["agent_definition_version"]
            if isinstance(record, RowMapping)
            else record.agent_definition_version
        ),
        max_total_bytes=int(
            record["max_total_bytes"] if isinstance(record, RowMapping) else record.max_total_bytes
        ),
        max_total_code_points=int(
            record["max_total_code_points"]
            if isinstance(record, RowMapping)
            else record.max_total_code_points
        ),
        actual_total_bytes=int(
            record["actual_total_bytes"]
            if isinstance(record, RowMapping)
            else record.actual_total_bytes
        ),
        actual_total_code_points=int(
            record["actual_total_code_points"]
            if isinstance(record, RowMapping)
            else record.actual_total_code_points
        ),
        rendered_context=str(
            record["rendered_context"]
            if isinstance(record, RowMapping)
            else record.rendered_context
        ),
        content_digest=bytes(
            record["content_digest"] if isinstance(record, RowMapping) else record.content_digest
        ),
        created_at=created,
        selected_memories=deserialize_selected_memories(str(mem_raw)) if mem_raw else (),
        injected_user_memory_text=str(inj_user_mem) if inj_user_mem is not None else None,
        injected_agent_memory_text=str(inj_agent_mem) if inj_agent_mem is not None else None,
    )


class SqlAlchemyConversationPersistence:
    """SQLAlchemy implementation of Conversation, Turn, Message, and Snapshot persistence."""

    def __init__(
        self,
        engine: Engine,
        *,
        capacity: int = 64,
        agent_capacity: int = 16,
        provider_capacity: int = 32,
        sleep: Callable[[float], None] | None = None,
        max_pending: int | None = None,
        max_pending_per_agent: int | None = None,
        max_pending_per_provider: int | None = None,
    ) -> None:
        self._engine = engine
        self._runner = TransactionRunner(engine, sleep=sleep if sleep is not None else time.sleep)
        self._capacity = max_pending if max_pending is not None else capacity
        self._agent_capacity = (
            max_pending_per_agent if max_pending_per_agent is not None else agent_capacity
        )
        self._provider_capacity = (
            max_pending_per_provider if max_pending_per_provider is not None else provider_capacity
        )
        self._sleep = sleep

    def create_conversation(
        self,
        owner_user_id: int,
        agent_instance_id: int,
        title: str | None,
        now: datetime,
    ) -> Conversation:
        def operation(connection: Connection) -> Conversation:
            # Verify agent instance is owned by the owner_user_id
            inst = connection.execute(
                select(AgentInstanceRecord.id).where(
                    AgentInstanceRecord.id == agent_instance_id,
                    AgentInstanceRecord.owner_user_id == owner_user_id,
                )
            ).scalar_one_or_none()
            if inst is None:
                raise ConversationNotFound(f"Agent instance {agent_instance_id} not found")

            stmt = (
                insert(ConversationRecord)
                .values(
                    owner_user_id=owner_user_id,
                    agent_instance_id=agent_instance_id,
                    title=title,
                    created_at=now,
                    updated_at=now,
                )
                .returning(ConversationRecord)
            )
            row = connection.execute(stmt).mappings().one()
            return _to_conversation(row)

        return self._runner.run(operation)

    def get_conversation(self, owner_user_id: int, conversation_id: int) -> Conversation:
        def operation(connection: Connection) -> Conversation:
            row = (
                connection.execute(
                    select(ConversationRecord).where(
                        ConversationRecord.id == conversation_id,
                        ConversationRecord.owner_user_id == owner_user_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ConversationNotFound(f"Conversation {conversation_id} not found")
            return _to_conversation(row)

        return self._runner.run(operation)

    def list_conversations(
        self,
        owner_user_id: int,
        limit: int,
        before_id: int | None,
        agent_instance_id: int | None = None,
    ) -> tuple[Conversation, ...]:
        def operation(connection: Connection) -> tuple[Conversation, ...]:
            conditions = [ConversationRecord.owner_user_id == owner_user_id]
            if before_id is not None:
                conditions.append(ConversationRecord.id < before_id)
            if agent_instance_id is not None:
                conditions.append(ConversationRecord.agent_instance_id == agent_instance_id)

            stmt = (
                select(ConversationRecord)
                .where(and_(*conditions))
                .order_by(ConversationRecord.id.desc())
                .limit(limit)
            )
            rows = connection.execute(stmt).mappings().all()
            return tuple(_to_conversation(row) for row in rows)

        return self._runner.run(operation)

    def _load_candidate_turns_on_connection(
        self, connection: Connection, conversation_id: int, before_sequence: int
    ) -> list[tuple[int, ConversationMessage, ConversationMessage]]:
        turn_rows = (
            connection.execute(
                select(ConversationTurnRecord)
                .where(
                    ConversationTurnRecord.conversation_id == conversation_id,
                    ConversationTurnRecord.sequence < before_sequence,
                    ConversationTurnRecord.state == TurnState.SUCCEEDED.value,
                )
                .order_by(ConversationTurnRecord.sequence.asc())
            )
            .mappings()
            .all()
        )
        if not turn_rows:
            return []

        turn_ids = [int(t["id"]) for t in turn_rows]
        msg_rows = (
            connection.execute(
                select(ConversationMessageRecord)
                .where(ConversationMessageRecord.turn_id.in_(turn_ids))
                .order_by(
                    ConversationMessageRecord.turn_id.asc(),
                    ConversationMessageRecord.role.desc(),
                )
            )
            .mappings()
            .all()
        )

        msgs_by_turn: dict[int, dict[str, ConversationMessage]] = {}
        for m in msg_rows:
            tid = int(m["turn_id"])
            if tid not in msgs_by_turn:
                msgs_by_turn[tid] = {}
            msgs_by_turn[tid][str(m["role"])] = _to_message(m)

        pairs: list[tuple[int, ConversationMessage, ConversationMessage]] = []
        for t in turn_rows:
            tid = int(t["id"])
            seq = int(t["sequence"])
            t_msgs = msgs_by_turn.get(tid, {})
            if MessageRole.USER.value in t_msgs and MessageRole.ASSISTANT.value in t_msgs:
                pairs.append(
                    (seq, t_msgs[MessageRole.USER.value], t_msgs[MessageRole.ASSISTANT.value])
                )
        return pairs

    def _load_current_compaction_on_connection(
        self, connection: Connection, conversation_id: int
    ) -> CompactionData | None:
        row = (
            connection.execute(
                select(ConversationCompactionRecord).where(
                    ConversationCompactionRecord.conversation_id == conversation_id,
                    ConversationCompactionRecord.is_current == True,  # noqa: E712
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return CompactionData(
            version=int(row["version"]),
            source_start_sequence=int(row["source_start_sequence"]),
            source_end_sequence=int(row["source_end_sequence"]),
            content=str(row["content"]),
            content_digest=bytes(row["content_digest"]),
        )

    def send_message(
        self,
        *,
        owner_user_id: int,
        conversation_id: int,
        agent_instance_id: int,
        client_message_id: str,
        content: str,
        content_digest: bytes,
        limits: RunLimits,
        definition_id: AgentDefinitionId,
        now: datetime,
        max_attempts: int = 3,
    ) -> tuple[ConversationTurnDetail, bool]:
        def operation(connection: Connection) -> tuple[ConversationTurnDetail, bool]:
            # 1. Verify conversation belongs to owner
            conv_row = (
                connection.execute(
                    select(ConversationRecord).where(
                        ConversationRecord.id == conversation_id,
                        ConversationRecord.owner_user_id == owner_user_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if conv_row is None:
                raise ConversationNotFound(f"Conversation {conversation_id} not found")

            # 2. Check idempotency: same client_message_id
            existing_turn = (
                connection.execute(
                    select(ConversationTurnRecord).where(
                        ConversationTurnRecord.conversation_id == conversation_id,
                        ConversationTurnRecord.client_message_id == client_message_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing_turn is not None:
                if bytes(existing_turn["content_digest"]) != content_digest:
                    raise ConversationConflict(
                        "client_message_id already used with different content"
                    )
                # Same client_message_id + same content: replay existing turn!
                detail = self._load_turn_detail_on_connection(
                    connection, conversation_id, int(existing_turn["id"])
                )
                return detail, True

            # 3. Check one-active-turn: no pending or running turn
            active_turn = connection.execute(
                select(ConversationTurnRecord.id).where(
                    ConversationTurnRecord.conversation_id == conversation_id,
                    ConversationTurnRecord.state.in_(("pending", "running")),
                )
            ).scalar_one_or_none()
            if active_turn is not None:
                raise ConversationBusy("Conversation has an active turn")

            # 4. Allocate next monotonic sequence
            max_seq = connection.execute(
                select(func.coalesce(func.max(ConversationTurnRecord.sequence), 0)).where(
                    ConversationTurnRecord.conversation_id == conversation_id
                )
            ).scalar_one()
            next_seq = int(max_seq) + 1

            # 4b. ContextBuilder assembly with memory retrieval on this connection
            candidate_turns = self._load_candidate_turns_on_connection(
                connection, conversation_id, next_seq
            )
            current_compaction = self._load_current_compaction_on_connection(
                connection, conversation_id
            )
            memory_candidates = _retrieve_memory_candidates_on_connection(
                connection,
                owner_user_id=owner_user_id,
                agent_instance_id=agent_instance_id,
                candidate_limit=50,
            )
            snapshot_data = ContextBuilder.assemble(
                current_user_text=content,
                candidate_turns=candidate_turns,
                current_compaction=current_compaction,
                agent_definition_id=definition_id,
                now=now,
                memory_candidates=memory_candidates,
                max_bytes=limits.input_max_bytes,
                max_code_points=limits.input_max_code_points,
            )

            # 5. Insert Turn Record
            turn_insert = (
                insert(ConversationTurnRecord)
                .values(
                    conversation_id=conversation_id,
                    sequence=next_seq,
                    state=TurnState.RUNNING.value,
                    client_message_id=client_message_id,
                    content_digest=content_digest,
                    authoritative_run_id=None,
                    created_at=now,
                    started_at=now,
                    finished_at=None,
                )
                .returning(ConversationTurnRecord)
            )
            try:
                turn_row = connection.execute(turn_insert).mappings().one()
            except IntegrityError as err:
                # Catch partial index or unique constraint violation on race
                raise ConversationBusy("Conversation has an active turn") from err

            turn_id = int(turn_row["id"])
            turn = _to_turn(turn_row)

            # 6. Insert USER message
            msg_insert = (
                insert(ConversationMessageRecord)
                .values(
                    turn_id=turn_id,
                    role=MessageRole.USER.value,
                    content=content,
                    source_run_id=None,
                    created_at=now,
                )
                .returning(ConversationMessageRecord)
            )
            user_msg_row = connection.execute(msg_insert).mappings().one()
            user_msg = _to_message(user_msg_row)

            # 7. Insert Run + Job via canonical helper with assembled context
            run = insert_run_and_job_on_connection(
                connection,
                owner_user_id=owner_user_id,
                agent_instance_id=agent_instance_id,
                input_text=snapshot_data.rendered_context,
                limits=limits,
                definition_id=definition_id,
                now=now,
                max_attempts=max_attempts,
                capacity=self._capacity,
                agent_capacity=self._agent_capacity,
                provider_capacity=self._provider_capacity,
            )

            # 8. Insert Run link with context_mode = f2_context_snapshot
            link_insert = insert(ConversationRunLinkRecord).values(
                turn_id=turn_id,
                run_id=run.id,
                ordinal=1,
                role=RunLinkRole.INITIAL.value,
                context_mode="f2_context_snapshot",
                created_at=now,
            )
            connection.execute(link_insert)

            # 8b. Insert RunContextSnapshot referencing (turn_id, run_id)
            snapshot_insert = insert(RunContextSnapshotRecord).values(
                run_id=run.id,
                turn_id=turn_id,
                schema_version=snapshot_data.schema_version,
                builder_version=snapshot_data.builder_version,
                current_user_text=snapshot_data.current_user_text,
                history_messages=serialize_history_messages(snapshot_data.history_messages),
                selected_turn_ids=serialize_id_list(snapshot_data.selected_turn_ids),
                selected_message_ids=serialize_id_list(snapshot_data.selected_message_ids),
                compaction_version=snapshot_data.compaction_version,
                compaction_source_start=snapshot_data.compaction_source_start,
                compaction_source_end=snapshot_data.compaction_source_end,
                injected_compaction_text=snapshot_data.injected_compaction_text,
                agent_key=snapshot_data.agent_key,
                agent_definition_version=snapshot_data.agent_definition_version,
                max_total_bytes=snapshot_data.max_total_bytes,
                max_total_code_points=snapshot_data.max_total_code_points,
                actual_total_bytes=snapshot_data.actual_total_bytes,
                actual_total_code_points=snapshot_data.actual_total_code_points,
                rendered_context=snapshot_data.rendered_context,
                content_digest=snapshot_data.content_digest,
                created_at=now,
                memory_items_json=serialize_selected_memories(snapshot_data.selected_memories),
                injected_user_memory_text=snapshot_data.injected_user_memory_text,
                injected_agent_memory_text=snapshot_data.injected_agent_memory_text,
            )
            connection.execute(snapshot_insert)

            # 9. Update conversation updated_at
            connection.execute(
                update(ConversationRecord)
                .where(ConversationRecord.id == conversation_id)
                .values(updated_at=now)
            )

            detail = ConversationTurnDetail(
                turn=turn,
                user_message=user_msg,
                assistant_message=None,
                latest_run_id=run.id,
                latest_run_status=run.status.value,
                is_retryable=False,
            )
            return detail, False

        return self._runner.run(operation)

    def _load_turn_detail_on_connection(
        self, connection: Connection, conversation_id: int, turn_id: int
    ) -> ConversationTurnDetail:
        turn_row = (
            connection.execute(
                select(ConversationTurnRecord).where(
                    ConversationTurnRecord.id == turn_id,
                    ConversationTurnRecord.conversation_id == conversation_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        if turn_row is None:
            raise TurnNotFound(f"Turn {turn_id} not found")

        turn = _to_turn(turn_row)

        msg_rows = (
            connection.execute(
                select(ConversationMessageRecord).where(
                    ConversationMessageRecord.turn_id == turn_id
                )
            )
            .mappings()
            .all()
        )
        user_msg: ConversationMessage | None = None
        assistant_msg: ConversationMessage | None = None
        for m in msg_rows:
            role = str(m["role"])
            if role == MessageRole.USER.value:
                user_msg = _to_message(m)
            elif role == MessageRole.ASSISTANT.value:
                assistant_msg = _to_message(m)

        assert user_msg is not None

        latest_link = (
            connection.execute(
                select(ConversationRunLinkRecord.run_id)
                .where(ConversationRunLinkRecord.turn_id == turn_id)
                .order_by(ConversationRunLinkRecord.ordinal.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )

        latest_run_id: int | None = None
        latest_run_status: str | None = None
        if latest_link is not None:
            latest_run_id = int(latest_link["run_id"])
            run_status = connection.execute(
                select(RunRecord.status).where(RunRecord.id == latest_run_id)
            ).scalar_one_or_none()
            if run_status is not None:
                latest_run_status = str(run_status)

        # Check if latest turn in conversation
        max_seq = connection.execute(
            select(func.max(ConversationTurnRecord.sequence)).where(
                ConversationTurnRecord.conversation_id == conversation_id
            )
        ).scalar_one_or_none()
        is_latest = max_seq is not None and turn.sequence == max_seq

        # Determine is_retryable: latest turn in conversation and state in (failed, cancelled)
        is_retryable = is_latest and turn.state in (TurnState.FAILED, TurnState.CANCELLED)

        return ConversationTurnDetail(
            turn=turn,
            user_message=user_msg,
            assistant_message=assistant_msg,
            latest_run_id=latest_run_id,
            latest_run_status=latest_run_status,
            is_retryable=is_retryable,
        )

    def get_turn_detail(
        self, owner_user_id: int, conversation_id: int, turn_id: int
    ) -> ConversationTurnDetail:
        def operation(connection: Connection) -> ConversationTurnDetail:
            # Check owner
            conv = connection.execute(
                select(ConversationRecord.id).where(
                    ConversationRecord.id == conversation_id,
                    ConversationRecord.owner_user_id == owner_user_id,
                )
            ).scalar_one_or_none()
            if conv is None:
                raise ConversationNotFound(f"Conversation {conversation_id} not found")

            return self._load_turn_detail_on_connection(connection, conversation_id, turn_id)

        return self._runner.run(operation)

    def list_turns(
        self,
        owner_user_id: int,
        conversation_id: int,
        limit: int,
        before_sequence: int | None,
    ) -> tuple[ConversationTurnDetail, ...]:
        def operation(connection: Connection) -> tuple[ConversationTurnDetail, ...]:
            conv = connection.execute(
                select(ConversationRecord.id).where(
                    ConversationRecord.id == conversation_id,
                    ConversationRecord.owner_user_id == owner_user_id,
                )
            ).scalar_one_or_none()
            if conv is None:
                raise ConversationNotFound(f"Conversation {conversation_id} not found")

            conditions = [ConversationTurnRecord.conversation_id == conversation_id]
            if before_sequence is not None:
                conditions.append(ConversationTurnRecord.sequence < before_sequence)

            turn_ids = (
                connection.execute(
                    select(ConversationTurnRecord.id)
                    .where(and_(*conditions))
                    .order_by(ConversationTurnRecord.sequence.desc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )

            return tuple(
                self._load_turn_detail_on_connection(connection, conversation_id, tid)
                for tid in turn_ids
            )

        return self._runner.run(operation)

    def retry_turn(
        self,
        *,
        owner_user_id: int,
        conversation_id: int,
        turn_id: int,
        agent_instance_id: int,
        limits: RunLimits,
        definition_id: AgentDefinitionId,
        now: datetime,
        max_attempts: int = 3,
    ) -> tuple[ConversationTurnDetail, bool]:
        def operation(connection: Connection) -> tuple[ConversationTurnDetail, bool]:
            # 1. Verify conversation belongs to owner
            conv = (
                connection.execute(
                    select(ConversationRecord).where(
                        ConversationRecord.id == conversation_id,
                        ConversationRecord.owner_user_id == owner_user_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if conv is None:
                raise ConversationNotFound(f"Conversation {conversation_id} not found")

            # 2. Select turn
            turn_row = (
                connection.execute(
                    select(ConversationTurnRecord).where(
                        ConversationTurnRecord.id == turn_id,
                        ConversationTurnRecord.conversation_id == conversation_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if turn_row is None:
                raise TurnNotFound(f"Turn {turn_id} not found")

            current_state = TurnState(str(turn_row["state"]))
            # If already running (concurrent or transport replay), return existing active retry turn
            if current_state is TurnState.RUNNING:
                detail = self._load_turn_detail_on_connection(connection, conversation_id, turn_id)
                return detail, True

            if current_state not in (TurnState.FAILED, TurnState.CANCELLED):
                raise TurnNotRetryable(f"Cannot retry turn in state {current_state.value}")

            # 3. Check turn is latest
            max_seq = connection.execute(
                select(func.max(ConversationTurnRecord.sequence)).where(
                    ConversationTurnRecord.conversation_id == conversation_id
                )
            ).scalar_one_or_none()
            turn_seq = int(turn_row["sequence"])
            if turn_seq != max_seq:
                raise TurnNotRetryable("Turn is not the latest turn in conversation")

            # 4. Check no other active turn exists
            other_active = connection.execute(
                select(ConversationTurnRecord.id).where(
                    ConversationTurnRecord.conversation_id == conversation_id,
                    ConversationTurnRecord.id != turn_id,
                    ConversationTurnRecord.state.in_(("pending", "running")),
                )
            ).scalar_one_or_none()
            if other_active is not None:
                raise ConversationBusy("Conversation has an active turn")

            # 5. Get next ordinal
            max_ord = connection.execute(
                select(func.coalesce(func.max(ConversationRunLinkRecord.ordinal), 0)).where(
                    ConversationRunLinkRecord.turn_id == turn_id
                )
            ).scalar_one()
            next_ordinal = int(max_ord) + 1

            # 6. Get original USER message content
            user_msg_row = (
                connection.execute(
                    select(ConversationMessageRecord).where(
                        ConversationMessageRecord.turn_id == turn_id,
                        ConversationMessageRecord.role == MessageRole.USER.value,
                    )
                )
                .mappings()
                .one()
            )
            user_content = str(user_msg_row["content"])

            # 6b. ContextBuilder assembly for retry
            candidate_turns = self._load_candidate_turns_on_connection(
                connection, conversation_id, turn_seq
            )
            current_compaction = self._load_current_compaction_on_connection(
                connection, conversation_id
            )
            memory_candidates = _retrieve_memory_candidates_on_connection(
                connection,
                owner_user_id=owner_user_id,
                agent_instance_id=agent_instance_id,
                candidate_limit=50,
            )
            snapshot_data = ContextBuilder.assemble(
                current_user_text=user_content,
                candidate_turns=candidate_turns,
                current_compaction=current_compaction,
                agent_definition_id=definition_id,
                now=now,
                memory_candidates=memory_candidates,
                max_bytes=limits.input_max_bytes,
                max_code_points=limits.input_max_code_points,
            )

            # 7. Transition Turn to RUNNING
            connection.execute(
                update(ConversationTurnRecord)
                .where(ConversationTurnRecord.id == turn_id)
                .values(
                    state=TurnState.RUNNING.value,
                    started_at=now,
                    finished_at=None,
                )
            )

            # 8. Insert new Run + Job via canonical helper with assembled context
            run = insert_run_and_job_on_connection(
                connection,
                owner_user_id=owner_user_id,
                agent_instance_id=agent_instance_id,
                input_text=snapshot_data.rendered_context,
                limits=limits,
                definition_id=definition_id,
                now=now,
                max_attempts=max_attempts,
                capacity=self._capacity,
                agent_capacity=self._agent_capacity,
                provider_capacity=self._provider_capacity,
            )

            # 9. Insert new Run link with context_mode = f2_context_snapshot
            link_insert = insert(ConversationRunLinkRecord).values(
                turn_id=turn_id,
                run_id=run.id,
                ordinal=next_ordinal,
                role=RunLinkRole.RETRY.value,
                context_mode="f2_context_snapshot",
                created_at=now,
            )
            connection.execute(link_insert)

            # 9b. Insert RunContextSnapshot referencing (turn_id, run_id)
            snapshot_insert = insert(RunContextSnapshotRecord).values(
                run_id=run.id,
                turn_id=turn_id,
                schema_version=snapshot_data.schema_version,
                builder_version=snapshot_data.builder_version,
                current_user_text=snapshot_data.current_user_text,
                history_messages=serialize_history_messages(snapshot_data.history_messages),
                selected_turn_ids=serialize_id_list(snapshot_data.selected_turn_ids),
                selected_message_ids=serialize_id_list(snapshot_data.selected_message_ids),
                compaction_version=snapshot_data.compaction_version,
                compaction_source_start=snapshot_data.compaction_source_start,
                compaction_source_end=snapshot_data.compaction_source_end,
                injected_compaction_text=snapshot_data.injected_compaction_text,
                agent_key=snapshot_data.agent_key,
                agent_definition_version=snapshot_data.agent_definition_version,
                max_total_bytes=snapshot_data.max_total_bytes,
                max_total_code_points=snapshot_data.max_total_code_points,
                actual_total_bytes=snapshot_data.actual_total_bytes,
                actual_total_code_points=snapshot_data.actual_total_code_points,
                rendered_context=snapshot_data.rendered_context,
                content_digest=snapshot_data.content_digest,
                created_at=now,
                memory_items_json=serialize_selected_memories(snapshot_data.selected_memories),
                injected_user_memory_text=snapshot_data.injected_user_memory_text,
                injected_agent_memory_text=snapshot_data.injected_agent_memory_text,
            )
            connection.execute(snapshot_insert)

            # 10. Update conversation updated_at
            connection.execute(
                update(ConversationRecord)
                .where(ConversationRecord.id == conversation_id)
                .values(updated_at=now)
            )

            detail = self._load_turn_detail_on_connection(connection, conversation_id, turn_id)
            return detail, False

        return self._runner.run(operation)

    def _project_terminal_run_on_connection(
        self,
        connection: Connection,
        *,
        run_id: int,
        status: str,
        output_text: str | None,
        error_code: str | None,
        now: datetime,
    ) -> bool:
        # 1. Find the turn linked to this run
        link_row = (
            connection.execute(
                select(ConversationRunLinkRecord).where(ConversationRunLinkRecord.run_id == run_id)
            )
            .mappings()
            .one_or_none()
        )
        if link_row is None:
            # Non-conversational Run: safe no-op
            return True

        turn_id = int(link_row["turn_id"])

        turn_row = (
            connection.execute(
                select(ConversationTurnRecord).where(ConversationTurnRecord.id == turn_id)
            )
            .mappings()
            .one()
        )

        turn_state = TurnState(str(turn_row["state"]))
        auth_run_id = turn_row["authoritative_run_id"]

        if status == RunStatus.SUCCEEDED.value:
            if output_text is None:
                return False

            if auth_run_id is not None:
                # Turn already has an authoritative successful Run.
                # If it's this run, projection is already finalized; otherwise another run won CAS.
                return auth_run_id == run_id

            # Defensive CAS update: only one successful Run can transition the Turn and win CAS
            cas_result = connection.execute(
                update(ConversationTurnRecord)
                .where(
                    ConversationTurnRecord.id == turn_id,
                    ConversationTurnRecord.authoritative_run_id.is_(None),
                    ConversationTurnRecord.state.in_(
                        (TurnState.PENDING.value, TurnState.RUNNING.value)
                    ),
                )
                .values(
                    authoritative_run_id=run_id,
                    state=TurnState.SUCCEEDED.value,
                    finished_at=now,
                )
            )
            if _rowcount(cas_result) == 0:
                # Lost the CAS race to another successful run on this turn
                return False

            # Insert exactly one ASSISTANT message
            msg_insert = insert(ConversationMessageRecord).values(
                turn_id=turn_id,
                role=MessageRole.ASSISTANT.value,
                content=output_text,
                source_run_id=run_id,
                created_at=now,
            )
            connection.execute(msg_insert)

            # Update conversation updated_at
            connection.execute(
                update(ConversationRecord)
                .where(ConversationRecord.id == int(turn_row["conversation_id"]))
                .values(updated_at=now)
            )
            return True

        elif status == RunStatus.FAILED.value:
            target_state = (
                TurnState.AMBIGUOUS.value
                if error_code == EXECUTION_OUTCOME_AMBIGUOUS
                else TurnState.FAILED.value
            )
            if turn_state in (TurnState.PENDING, TurnState.RUNNING):
                connection.execute(
                    update(ConversationTurnRecord)
                    .where(
                        ConversationTurnRecord.id == turn_id,
                        ConversationTurnRecord.state.in_(
                            (TurnState.PENDING.value, TurnState.RUNNING.value)
                        ),
                    )
                    .values(state=target_state, finished_at=now)
                )
            return True

        elif status == RunStatus.CANCELLED.value:
            if turn_state in (TurnState.PENDING, TurnState.RUNNING):
                connection.execute(
                    update(ConversationTurnRecord)
                    .where(
                        ConversationTurnRecord.id == turn_id,
                        ConversationTurnRecord.state.in_(
                            (TurnState.PENDING.value, TurnState.RUNNING.value)
                        ),
                    )
                    .values(state=TurnState.CANCELLED.value, finished_at=now)
                )
            return True

        return False

    def project_terminal_run(
        self,
        *,
        run_id: int,
        status: str,
        output_text: str | None,
        error_code: str | None,
        now: datetime,
    ) -> bool:
        projected = self._runner.run(
            lambda connection: self._project_terminal_run_on_connection(
                connection,
                run_id=run_id,
                status=status,
                output_text=output_text,
                error_code=error_code,
                now=now,
            )
        )
        if projected and status == RunStatus.SUCCEEDED.value:
            # Best-effort refresh compaction in separate transaction
            try:
                with self._engine.connect() as conn:
                    cid = conn.scalar(
                        select(ConversationTurnRecord.conversation_id)
                        .join(
                            ConversationRunLinkRecord,
                            ConversationRunLinkRecord.turn_id == ConversationTurnRecord.id,
                        )
                        .where(ConversationRunLinkRecord.run_id == run_id)
                    )
                if cid is not None:
                    self.refresh_compaction(int(cid), now=now)
            except Exception:
                pass
        return projected

    def reconcile_unprojected_terminal_runs(self, now: datetime, limit: int = 50) -> int:
        def operation(connection: Connection) -> int:
            candidates = (
                connection.execute(
                    select(
                        ConversationTurnRecord.id.label("turn_id"),
                        ConversationRunLinkRecord.run_id,
                        RunRecord.status,
                        RunRecord.output_text,
                        RunRecord.error_code,
                    )
                    .join(
                        ConversationRunLinkRecord,
                        ConversationRunLinkRecord.turn_id == ConversationTurnRecord.id,
                    )
                    .join(RunRecord, RunRecord.id == ConversationRunLinkRecord.run_id)
                    .where(
                        ConversationTurnRecord.state.in_(
                            (TurnState.PENDING.value, TurnState.RUNNING.value)
                        ),
                        RunRecord.status.in_(
                            (
                                RunStatus.SUCCEEDED.value,
                                RunStatus.FAILED.value,
                                RunStatus.CANCELLED.value,
                            )
                        ),
                    )
                    .order_by(
                        ConversationRunLinkRecord.turn_id,
                        ConversationRunLinkRecord.ordinal.desc(),
                    )
                    .limit(limit)
                )
                .mappings()
                .all()
            )

            reconciled = 0
            for candidate in candidates:
                run_id = int(candidate["run_id"])
                status = str(candidate["status"])
                output_text = candidate["output_text"]
                error_code = candidate["error_code"]
                projected = self._project_terminal_run_on_connection(
                    connection,
                    run_id=run_id,
                    status=status,
                    output_text=output_text,
                    error_code=error_code,
                    now=now,
                )
                if projected:
                    reconciled += 1
            return reconciled

        return self._runner.run(operation)

    def load_run_context_snapshot(self, run_id: int) -> ContextSnapshotData | None:
        def operation(connection: Connection) -> ContextSnapshotData | None:
            row = (
                connection.execute(
                    select(RunContextSnapshotRecord).where(
                        RunContextSnapshotRecord.run_id == run_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            return _to_context_snapshot(row)

        return self._runner.run(operation)

    def load_conversation_run_link(self, run_id: int) -> tuple[int, int, int, int, str, str] | None:
        def operation(connection: Connection) -> tuple[int, int, int, int, str, str] | None:
            row = (
                connection.execute(
                    select(ConversationRunLinkRecord).where(
                        ConversationRunLinkRecord.run_id == run_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            return (
                int(row["id"]),
                int(row["turn_id"]),
                int(row["run_id"]),
                int(row["ordinal"]),
                str(row["role"]),
                str(row["context_mode"]),
            )

        return self._runner.run(operation)

    def refresh_compaction(self, conversation_id: int, now: datetime) -> bool:
        def operation(connection: Connection) -> bool:
            # 1. Determine aged-out cutoff boundary (turns older than the 10 newest succeeded turns)
            succeeded_sequences = (
                connection.execute(
                    select(ConversationTurnRecord.sequence)
                    .where(
                        ConversationTurnRecord.conversation_id == conversation_id,
                        ConversationTurnRecord.state == TurnState.SUCCEEDED.value,
                    )
                    .order_by(ConversationTurnRecord.sequence.asc())
                )
                .scalars()
                .all()
            )
            if len(succeeded_sequences) <= 10:
                # No turns have aged out of the 10-turn (20-message) recent window yet
                return False

            aged_out_succeeded = succeeded_sequences[:-10]
            aged_out_cutoff = int(aged_out_succeeded[-1])

            current = (
                connection.execute(
                    select(ConversationCompactionRecord).where(
                        ConversationCompactionRecord.conversation_id == conversation_id,
                        ConversationCompactionRecord.is_current == True,  # noqa: E712
                    )
                )
                .mappings()
                .one_or_none()
            )
            source_end = int(current["source_end_sequence"]) if current else 0
            source_start = int(current["source_start_sequence"]) if current else 1

            if source_end >= aged_out_cutoff:
                return False

            new_turns = (
                connection.execute(
                    select(ConversationTurnRecord)
                    .where(
                        ConversationTurnRecord.conversation_id == conversation_id,
                        ConversationTurnRecord.sequence > source_end,
                        ConversationTurnRecord.sequence <= aged_out_cutoff,
                        ConversationTurnRecord.state == TurnState.SUCCEEDED.value,
                    )
                    .order_by(ConversationTurnRecord.sequence.asc())
                    .limit(50)
                )
                .mappings()
                .all()
            )
            if not new_turns:
                return False

            new_turn_ids = [int(t["id"]) for t in new_turns]
            msg_rows = (
                connection.execute(
                    select(ConversationMessageRecord)
                    .where(ConversationMessageRecord.turn_id.in_(new_turn_ids))
                    .order_by(
                        ConversationMessageRecord.turn_id.asc(),
                        ConversationMessageRecord.role.desc(),
                    )
                )
                .mappings()
                .all()
            )
            msgs_by_turn: dict[int, dict[str, str]] = {}
            for m in msg_rows:
                tid = int(m["turn_id"])
                if tid not in msgs_by_turn:
                    msgs_by_turn[tid] = {}
                msgs_by_turn[tid][str(m["role"])] = str(m["content"])

            new_pairs: list[tuple[int, str, str]] = []
            for t in new_turns:
                tid = int(t["id"])
                seq = int(t["sequence"])
                t_msgs = msgs_by_turn.get(tid, {})
                if MessageRole.USER.value in t_msgs and MessageRole.ASSISTANT.value in t_msgs:
                    new_pairs.append(
                        (seq, t_msgs[MessageRole.USER.value], t_msgs[MessageRole.ASSISTANT.value])
                    )

            if not new_pairs:
                return False

            new_turn_blocks = [f"Turn {seq}:\nUser: {u}\nAssistant: {a}" for seq, u, a in new_pairs]

            existing_blocks: list[str] = []
            if current is not None:
                stored_body = str(current["content"])
                if stored_body.startswith(COMPACTION_HEADER_STORED + "\n"):
                    stored_body = stored_body[len(COMPACTION_HEADER_STORED) + 1 :]
                elif stored_body.startswith(COMPACTION_HEADER_STORED):
                    stored_body = stored_body[len(COMPACTION_HEADER_STORED) :].lstrip("\n")
                existing_blocks = [b.strip() for b in stored_body.split("\n\n") if b.strip()]

            combined_blocks = existing_blocks + new_turn_blocks
            new_source_end = new_pairs[-1][0]
            new_source_start = source_start if current is not None else new_pairs[0][0]

            while (
                len(("\n\n".join(combined_blocks)).encode("utf-8")) > 31000
                and len(combined_blocks) > 1
            ):
                combined_blocks.pop(0)
                if combined_blocks and combined_blocks[0].startswith("Turn "):
                    try:
                        first_line = combined_blocks[0].split("\n", 1)[0]
                        new_source_start = int(
                            first_line.replace("Turn ", "").replace(":", "").strip()
                        )
                    except ValueError:
                        pass

            stored_content = f"{COMPACTION_HEADER_STORED}\n" + "\n\n".join(combined_blocks)
            digest = compute_context_digest(stored_content)
            new_version = (int(current["version"]) + 1) if current else 1

            if current is not None:
                connection.execute(
                    update(ConversationCompactionRecord)
                    .where(ConversationCompactionRecord.id == int(current["id"]))
                    .values(is_current=False)
                )

            connection.execute(
                insert(ConversationCompactionRecord).values(
                    conversation_id=conversation_id,
                    version=new_version,
                    source_start_sequence=new_source_start,
                    source_end_sequence=new_source_end,
                    content=stored_content,
                    content_digest=digest,
                    is_current=True,
                    created_at=now,
                )
            )
            return True

        return self._runner.run(operation)
