"""Owner-scoped Conversation, Turn, Message, and RunLink persistence."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import Engine, and_, func, insert, select, update
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import IntegrityError

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
    validate_pending_cap,
)
from nervos_core.infrastructure.database.models import (
    AgentInstanceRecord,
    ConversationMessageRecord,
    ConversationRecord,
    ConversationRunLinkRecord,
    ConversationTurnRecord,
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
    assert created is not None and updated is not None
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
        title=record["title"] if isinstance(record, RowMapping) else record.title,
        created_at=created,
        updated_at=updated,
    )


def _to_turn(record: ConversationTurnRecord | RowMapping) -> ConversationTurn:
    created = _as_utc(record["created_at"] if isinstance(record, RowMapping) else record.created_at)
    assert created is not None
    started = _as_utc(record["started_at"] if isinstance(record, RowMapping) else record.started_at)
    finished = _as_utc(
        record["finished_at"] if isinstance(record, RowMapping) else record.finished_at
    )
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


class SqlAlchemyConversationPersistence:
    """Owner-scoped persistence implementation for Conversations."""

    def __init__(
        self,
        engine: Engine,
        *,
        max_pending: int = 1000,
        max_pending_per_agent: int | None = None,
        max_pending_per_provider: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._engine = engine
        self._runner = TransactionRunner(engine, sleep=sleep)
        self._capacity = validate_pending_cap(max_pending, "max_pending")
        self._agent_capacity = validate_pending_cap(
            self._capacity if max_pending_per_agent is None else max_pending_per_agent,
            "max_pending_per_agent",
        )
        self._provider_capacity = validate_pending_cap(
            self._capacity if max_pending_per_provider is None else max_pending_per_provider,
            "max_pending_per_provider",
        )

    def create_conversation(
        self,
        owner_user_id: int,
        agent_instance_id: int,
        title: str | None,
        now: datetime,
    ) -> Conversation:
        def operation(connection: Connection) -> Conversation:
            # Verify agent instance exists and belongs to owner
            instance_exists = connection.execute(
                select(AgentInstanceRecord.id).where(
                    AgentInstanceRecord.id == agent_instance_id,
                    AgentInstanceRecord.owner_user_id == owner_user_id,
                )
            ).scalar_one_or_none()
            if instance_exists is None:
                raise ConversationNotFound("Agent instance not found for owner")

            insert_stmt = (
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
            row = connection.execute(insert_stmt).mappings().one()
            return _to_conversation(row)

        return self._runner.run(operation)

    def get_conversation(self, owner_user_id: int, conversation_id: int) -> Conversation:
        with self._engine.connect() as connection:
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

    def list_conversations(
        self,
        owner_user_id: int,
        limit: int,
        before_id: int | None,
        agent_instance_id: int | None = None,
    ) -> tuple[Conversation, ...]:
        with self._engine.connect() as connection:
            predicates = [ConversationRecord.owner_user_id == owner_user_id]
            if agent_instance_id is not None:
                predicates.append(ConversationRecord.agent_instance_id == agent_instance_id)
            if before_id is not None:
                predicates.append(ConversationRecord.id < before_id)

            rows = (
                connection.execute(
                    select(ConversationRecord)
                    .where(and_(*predicates))
                    .order_by(ConversationRecord.id.desc())
                    .limit(limit)
                )
                .mappings()
                .all()
            )
            return tuple(_to_conversation(row) for row in rows)

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

            # 7. Insert Run + Job via the canonical helper on this connection
            run = insert_run_and_job_on_connection(
                connection,
                owner_user_id=owner_user_id,
                agent_instance_id=agent_instance_id,
                input_text=content,
                limits=limits,
                definition_id=definition_id,
                now=now,
                max_attempts=max_attempts,
                capacity=self._capacity,
                agent_capacity=self._agent_capacity,
                provider_capacity=self._provider_capacity,
            )

            # 8. Insert Run link
            link_insert = insert(ConversationRunLinkRecord).values(
                turn_id=turn_id,
                run_id=run.id,
                ordinal=1,
                role=RunLinkRole.INITIAL.value,
                created_at=now,
            )
            connection.execute(link_insert)

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

        messages = (
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
        for m in messages:
            msg_obj = _to_message(m)
            if msg_obj.role is MessageRole.USER:
                user_msg = msg_obj
            elif msg_obj.role is MessageRole.ASSISTANT:
                assistant_msg = msg_obj
        assert user_msg is not None, f"Turn {turn_id} has no USER message"

        latest_link = (
            connection.execute(
                select(ConversationRunLinkRecord, RunRecord.status)
                .join(RunRecord, RunRecord.id == ConversationRunLinkRecord.run_id)
                .where(ConversationRunLinkRecord.turn_id == turn_id)
                .order_by(ConversationRunLinkRecord.ordinal.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )

        latest_run_id = int(latest_link["run_id"]) if latest_link else None
        latest_run_status = str(latest_link["status"]) if latest_link else None

        # Determine is_retryable: latest turn in conversation and state in (failed, cancelled)
        max_seq = connection.execute(
            select(func.max(ConversationTurnRecord.sequence)).where(
                ConversationTurnRecord.conversation_id == conversation_id
            )
        ).scalar_one_or_none()
        is_latest = max_seq == turn.sequence
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
        with self._engine.connect() as connection:
            conv = connection.execute(
                select(ConversationRecord.id).where(
                    ConversationRecord.id == conversation_id,
                    ConversationRecord.owner_user_id == owner_user_id,
                )
            ).scalar_one_or_none()
            if conv is None:
                raise ConversationNotFound(f"Conversation {conversation_id} not found")
            return self._load_turn_detail_on_connection(connection, conversation_id, turn_id)

    def list_turns(
        self,
        owner_user_id: int,
        conversation_id: int,
        limit: int,
        before_sequence: int | None,
    ) -> tuple[ConversationTurnDetail, ...]:
        with self._engine.connect() as connection:
            conv = connection.execute(
                select(ConversationRecord.id).where(
                    ConversationRecord.id == conversation_id,
                    ConversationRecord.owner_user_id == owner_user_id,
                )
            ).scalar_one_or_none()
            if conv is None:
                raise ConversationNotFound(f"Conversation {conversation_id} not found")

            predicates = [ConversationTurnRecord.conversation_id == conversation_id]
            if before_sequence is not None:
                predicates.append(ConversationTurnRecord.sequence < before_sequence)

            turn_rows = (
                connection.execute(
                    select(ConversationTurnRecord)
                    .where(and_(*predicates))
                    .order_by(ConversationTurnRecord.sequence.desc())
                    .limit(limit)
                )
                .mappings()
                .all()
            )

            return tuple(
                self._load_turn_detail_on_connection(connection, conversation_id, int(r["id"]))
                for r in turn_rows
            )

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
            if int(turn_row["sequence"]) != max_seq:
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

            # 7. Insert new Run + Job via the canonical helper
            run = insert_run_and_job_on_connection(
                connection,
                owner_user_id=owner_user_id,
                agent_instance_id=agent_instance_id,
                input_text=user_content,
                limits=limits,
                definition_id=definition_id,
                now=now,
                max_attempts=max_attempts,
                capacity=self._capacity,
                agent_capacity=self._agent_capacity,
                provider_capacity=self._provider_capacity,
            )

            # 8. Insert new Run link
            link_insert = insert(ConversationRunLinkRecord).values(
                turn_id=turn_id,
                run_id=run.id,
                ordinal=next_ordinal,
                role=RunLinkRole.RETRY.value,
                created_at=now,
            )
            connection.execute(link_insert)

            # 9. Update Turn state to RUNNING, clear finished_at / authoritative_run_id
            connection.execute(
                update(ConversationTurnRecord)
                .where(ConversationTurnRecord.id == turn_id)
                .values(
                    state=TurnState.RUNNING.value,
                    authoritative_run_id=None,
                    finished_at=None,
                )
            )

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
        link_row = (
            connection.execute(
                select(ConversationRunLinkRecord).where(ConversationRunLinkRecord.run_id == run_id)
            )
            .mappings()
            .one_or_none()
        )
        if link_row is None:
            return False  # Non-conversational run

        turn_id = int(link_row["turn_id"])
        turn_row = (
            connection.execute(
                select(ConversationTurnRecord).where(ConversationTurnRecord.id == turn_id)
            )
            .mappings()
            .one_or_none()
        )
        if turn_row is None:
            return False

        if status == RunStatus.SUCCEEDED.value:
            # Load output_text from runs table if not provided
            resolved_output = output_text
            if resolved_output is None:
                resolved_output = connection.execute(
                    select(RunRecord.output_text).where(RunRecord.id == run_id)
                ).scalar_one_or_none()
            if resolved_output is None:
                return False

            # Idempotent CAS: set authoritative_run_id only if None and state in (pending, running)
            cas_update = connection.execute(
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
            if _rowcount(cas_update) == 1:
                # Insert ASSISTANT message
                msg_insert = insert(ConversationMessageRecord).values(
                    turn_id=turn_id,
                    role=MessageRole.ASSISTANT.value,
                    content=resolved_output,
                    source_run_id=run_id,
                    created_at=now,
                )
                connection.execute(msg_insert)
                return True
            else:
                # Check if already finalized with this run_id
                existing_auth = turn_row["authoritative_run_id"]
                return existing_auth == run_id

        elif status == RunStatus.FAILED.value:
            target_state = (
                TurnState.AMBIGUOUS.value
                if error_code == EXECUTION_OUTCOME_AMBIGUOUS
                else TurnState.FAILED.value
            )
            # Only update if this is the latest linked run for this turn
            max_ord = connection.execute(
                select(func.max(ConversationRunLinkRecord.ordinal)).where(
                    ConversationRunLinkRecord.turn_id == turn_id
                )
            ).scalar_one()
            if int(link_row["ordinal"]) == max_ord:
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
            max_ord = connection.execute(
                select(func.max(ConversationRunLinkRecord.ordinal)).where(
                    ConversationRunLinkRecord.turn_id == turn_id
                )
            ).scalar_one()
            if int(link_row["ordinal"]) == max_ord:
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
        return self._runner.run(
            lambda connection: self._project_terminal_run_on_connection(
                connection,
                run_id=run_id,
                status=status,
                output_text=output_text,
                error_code=error_code,
                now=now,
            )
        )

    def reconcile_unprojected_terminal_runs(self, now: datetime, limit: int = 50) -> int:
        def operation(connection: Connection) -> int:
            # Find turns in pending or running state that have a linked terminal run
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
                        ConversationRunLinkRecord.turn_id, ConversationRunLinkRecord.ordinal.desc()
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
