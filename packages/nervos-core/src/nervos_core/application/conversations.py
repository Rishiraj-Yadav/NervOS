"""Conversation application services and persistence contracts (Stage F1)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from nervos_core.application.agents import AgentService
from nervos_core.application.clock import Clock, require_utc
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.context import ContextSnapshotData
from nervos_core.domain.conversations import (
    Conversation,
    ConversationMessage,
    ConversationTurn,
    TurnState,
    compute_content_digest,
    validate_client_message_id,
    validate_conversation_title,
    validate_message_content,
)
from nervos_core.domain.runs import RunLimits


class ConversationNotFound(LookupError):
    """Raised when a Conversation does not exist or belongs to another owner."""


class ConversationBusy(Exception):
    """Raised when a Conversation already has an active (pending/running) turn."""


class ConversationConflict(Exception):
    """Raised when the same client_message_id is reused with different message content."""


class TurnNotFound(LookupError):
    """Raised when a Turn does not exist in the specified conversation."""


class TurnNotRetryable(Exception):
    """Raised when a Turn is not eligible for manual retry (not latest, or not failed/cancelled)."""


@dataclass(frozen=True, slots=True)
class ConversationTurnDetail:
    """Turn projection including its messages and execution status for history presentation."""

    turn: ConversationTurn
    user_message: ConversationMessage
    assistant_message: ConversationMessage | None
    latest_run_id: int | None
    latest_run_status: str | None
    is_retryable: bool


class ConversationPersistence(Protocol):
    """Persistence contract for Conversation execution protocol storage."""

    def create_conversation(
        self,
        owner_user_id: int,
        agent_instance_id: int,
        title: str | None,
        now: datetime,
    ) -> Conversation: ...

    def get_conversation(self, owner_user_id: int, conversation_id: int) -> Conversation: ...

    def list_conversations(
        self,
        owner_user_id: int,
        limit: int,
        before_id: int | None,
        agent_instance_id: int | None = None,
    ) -> tuple[Conversation, ...]: ...

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
    ) -> tuple[ConversationTurnDetail, bool]: ...

    def list_turns(
        self,
        owner_user_id: int,
        conversation_id: int,
        limit: int,
        before_sequence: int | None,
    ) -> tuple[ConversationTurnDetail, ...]: ...

    def get_turn_detail(
        self, owner_user_id: int, conversation_id: int, turn_id: int
    ) -> ConversationTurnDetail: ...

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
    ) -> tuple[ConversationTurnDetail, bool]: ...

    def project_terminal_run(
        self,
        *,
        run_id: int,
        status: str,
        output_text: str | None,
        error_code: str | None,
        now: datetime,
    ) -> bool: ...

    def reconcile_unprojected_terminal_runs(self, now: datetime, limit: int = 50) -> int: ...

    def load_run_context_snapshot(self, run_id: int) -> ContextSnapshotData | None: ...

    def load_conversation_run_link(
        self, run_id: int
    ) -> tuple[int, int, int, int, str, str] | None: ...

    def refresh_compaction(self, conversation_id: int, now: datetime) -> bool: ...


class ConversationService:
    """Owner-scoped application service for Conversations, Turns, and Messages."""

    def __init__(
        self,
        persistence: ConversationPersistence,
        agents: AgentService,
        *,
        clock: Clock,
    ) -> None:
        self._persistence = persistence
        self._agents = agents
        self._clock = clock

    def create_conversation(
        self,
        owner_user_id: int,
        agent_instance_id: int,
        title: str | None = None,
    ) -> Conversation:
        validated_title = validate_conversation_title(title)
        # Pre-flight check that instance exists and is owned by the user
        self._agents.get_instance(owner_user_id, agent_instance_id)
        now = require_utc(self._clock())
        return self._persistence.create_conversation(
            owner_user_id=owner_user_id,
            agent_instance_id=agent_instance_id,
            title=validated_title,
            now=now,
        )

    def get_conversation(self, owner_user_id: int, conversation_id: int) -> Conversation:
        return self._persistence.get_conversation(owner_user_id, conversation_id)

    def list_conversations(
        self,
        owner_user_id: int,
        limit: int = 20,
        before_id: int | None = None,
        agent_instance_id: int | None = None,
    ) -> tuple[Conversation, ...]:
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        if before_id is not None and before_id <= 0:
            raise ValueError("before_id must be positive")
        if agent_instance_id is not None:
            # Pre-flight ownership check
            self._agents.get_instance(owner_user_id, agent_instance_id)
        return self._persistence.list_conversations(
            owner_user_id, limit, before_id, agent_instance_id=agent_instance_id
        )

    def send_message(
        self,
        owner_user_id: int,
        conversation_id: int,
        client_message_id: str,
        content: str,
    ) -> tuple[ConversationTurnDetail, bool]:
        validated_client_id = validate_client_message_id(client_message_id)
        validated_content = validate_message_content(content)
        conversation = self._persistence.get_conversation(owner_user_id, conversation_id)
        instance, limits = self._agents.prepare_submission(
            owner_user_id, conversation.agent_instance_id, validated_content
        )
        digest = compute_content_digest(validated_content)
        now = require_utc(self._clock())
        return self._persistence.send_message(
            owner_user_id=owner_user_id,
            conversation_id=conversation_id,
            agent_instance_id=conversation.agent_instance_id,
            client_message_id=validated_client_id,
            content=validated_content,
            content_digest=digest,
            limits=limits,
            definition_id=instance.definition_id,
            now=now,
        )

    def list_turns(
        self,
        owner_user_id: int,
        conversation_id: int,
        limit: int = 20,
        before_sequence: int | None = None,
    ) -> tuple[ConversationTurnDetail, ...]:
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        if before_sequence is not None and before_sequence <= 0:
            raise ValueError("before_sequence must be positive")
        self._persistence.get_conversation(owner_user_id, conversation_id)
        return self._persistence.list_turns(owner_user_id, conversation_id, limit, before_sequence)

    def retry_turn(
        self,
        owner_user_id: int,
        conversation_id: int,
        turn_id: int,
    ) -> tuple[ConversationTurnDetail, bool]:
        conversation = self._persistence.get_conversation(owner_user_id, conversation_id)
        turn_detail = self._persistence.get_turn_detail(owner_user_id, conversation_id, turn_id)
        if not turn_detail.is_retryable and turn_detail.turn.state != TurnState.RUNNING:
            raise TurnNotRetryable("Turn is not eligible for manual retry")

        instance, limits = self._agents.prepare_submission(
            owner_user_id,
            conversation.agent_instance_id,
            turn_detail.user_message.content,
        )
        now = require_utc(self._clock())
        return self._persistence.retry_turn(
            owner_user_id=owner_user_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            agent_instance_id=conversation.agent_instance_id,
            limits=limits,
            definition_id=instance.definition_id,
            now=now,
        )

    def project_terminal_run(
        self,
        run_id: int,
        status: str,
        output_text: str | None = None,
        error_code: str | None = None,
    ) -> bool:
        now = require_utc(self._clock())
        return self._persistence.project_terminal_run(
            run_id=run_id,
            status=status,
            output_text=output_text,
            error_code=error_code,
            now=now,
        )

    def reconcile_unprojected_terminal_runs(self, limit: int = 50) -> int:
        now = require_utc(self._clock())
        return self._persistence.reconcile_unprojected_terminal_runs(now, limit=limit)

    def refresh_compaction(self, conversation_id: int) -> bool:
        now = require_utc(self._clock())
        return self._persistence.refresh_compaction(conversation_id, now=now)
