"""Memory application services, retrieval port, and persistence contracts (Stage F3)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from nervos_core.application.agents import AgentService
from nervos_core.application.clock import Clock, require_utc
from nervos_core.domain.memory import (
    InvalidMemory,
    InvalidMemorySource,
    MemoryItemDetail,
    MemoryProvenanceType,
    MemoryScope,
    MemorySourceKind,
    MemoryVersion,
    compute_memory_digest,
    validate_memory_content,
    validate_memory_scope,
)


@dataclass(frozen=True, slots=True)
class MemoryRetrievalQuery:
    owner_user_id: int
    agent_instance_id: int
    candidate_limit: int = 50
    user_text: str | None = None


@dataclass(frozen=True, slots=True)
class RetrievedMemoryItem:
    item_id: int
    version: int
    scope: MemoryScope
    provenance_type: MemoryProvenanceType
    content: str
    content_digest: bytes
    created_at: datetime


class MemoryRetrievalPort(Protocol):
    """Provider-neutral port for retrieving active memory candidates."""

    def retrieve_candidates(
        self, query: MemoryRetrievalQuery
    ) -> tuple[RetrievedMemoryItem, ...]: ...


class MemoryPersistence(Protocol):
    """Persistence contract for durable Memory storage."""

    def create_memory(
        self,
        *,
        owner_user_id: int,
        scope: MemoryScope,
        content: str,
        content_digest: bytes,
        agent_instance_id: int | None,
        source_kind: MemorySourceKind,
        source_id: int | None,
        provenance_type: MemoryProvenanceType,
        now: datetime,
    ) -> MemoryItemDetail: ...

    def promote_memory(
        self,
        *,
        owner_user_id: int,
        source_type: str,
        source_id: int,
        scope: MemoryScope,
        content: str | None,
        agent_instance_id: int | None,
        now: datetime,
    ) -> MemoryItemDetail: ...

    def get_memory(self, owner_user_id: int, memory_item_id: int) -> MemoryItemDetail: ...

    def list_memories(
        self,
        *,
        owner_user_id: int,
        scope: MemoryScope | None = None,
        agent_instance_id: int | None = None,
        before_id: int | None = None,
        limit: int = 20,
    ) -> tuple[tuple[MemoryItemDetail, ...], int | None]: ...

    def list_memory_versions(
        self,
        *,
        owner_user_id: int,
        memory_item_id: int,
        before_version: int | None = None,
        limit: int = 20,
    ) -> tuple[tuple[MemoryVersion, ...], int | None]: ...

    def edit_memory(
        self,
        *,
        owner_user_id: int,
        memory_item_id: int,
        expected_version: int,
        content: str,
        content_digest: bytes,
        now: datetime,
    ) -> MemoryItemDetail: ...

    def delete_memory(
        self,
        *,
        owner_user_id: int,
        memory_item_id: int,
        expected_version: int | None = None,
        now: datetime,
    ) -> None: ...

    def retrieve_candidates(
        self, query: MemoryRetrievalQuery
    ) -> tuple[RetrievedMemoryItem, ...]: ...


class MemoryService:
    """Owner-scoped application service for explicit memory creation, inspection, and lifecycle."""

    def __init__(
        self,
        persistence: MemoryPersistence,
        agents: AgentService,
        *,
        clock: Clock,
    ) -> None:
        self._persistence = persistence
        self._agents = agents
        self._clock = clock

    def create_memory(
        self,
        *,
        owner_user_id: int,
        scope: str | MemoryScope,
        content: str,
        agent_instance_id: int | None = None,
    ) -> MemoryItemDetail:
        """Create direct user-authored memory."""
        valid_scope = validate_memory_scope(str(scope))
        valid_content = validate_memory_content(content)
        digest = compute_memory_digest(valid_content)

        if valid_scope is MemoryScope.AGENT:
            if agent_instance_id is None:
                raise InvalidMemory("agent_instance_id is required for AGENT scope memory")
            # Pre-flight verify owner owns this agent instance
            self._agents.get_instance(owner_user_id, agent_instance_id)
        elif valid_scope is MemoryScope.USER and agent_instance_id is not None:
            raise InvalidMemory("agent_instance_id must be None for USER scope memory")

        now = require_utc(self._clock())
        return self._persistence.create_memory(
            owner_user_id=owner_user_id,
            scope=valid_scope,
            content=valid_content,
            content_digest=digest,
            agent_instance_id=agent_instance_id,
            source_kind=MemorySourceKind.DIRECT_USER,
            source_id=None,
            provenance_type=MemoryProvenanceType.USER_AUTHORED,
            now=now,
        )

    def promote_memory(
        self,
        *,
        owner_user_id: int,
        source_type: Literal["conversation_message", "run"] | str,
        source_id: int,
        scope: str | MemoryScope,
        content: str | None = None,
        agent_instance_id: int | None = None,
    ) -> MemoryItemDetail:
        """Explicitly promote a conversation message or succeeded run output to memory."""
        if source_type not in ("conversation_message", "run"):
            raise InvalidMemorySource(
                f"invalid source_type: {source_type}. Must be 'conversation_message' or 'run'"
            )
        if source_id <= 0:
            raise InvalidMemorySource("source_id must be a positive integer")

        valid_scope = validate_memory_scope(str(scope))
        if content is not None:
            content = validate_memory_content(content)

        if valid_scope is MemoryScope.AGENT:
            if agent_instance_id is None:
                raise InvalidMemory("agent_instance_id is required for AGENT scope memory")
            self._agents.get_instance(owner_user_id, agent_instance_id)
        elif valid_scope is MemoryScope.USER and agent_instance_id is not None:
            raise InvalidMemory("agent_instance_id must be None for USER scope memory")

        now = require_utc(self._clock())
        return self._persistence.promote_memory(
            owner_user_id=owner_user_id,
            source_type=str(source_type),
            source_id=source_id,
            scope=valid_scope,
            content=content,
            agent_instance_id=agent_instance_id,
            now=now,
        )

    def get_memory(self, owner_user_id: int, memory_item_id: int) -> MemoryItemDetail:
        return self._persistence.get_memory(owner_user_id, memory_item_id)

    def list_memories(
        self,
        *,
        owner_user_id: int,
        scope: str | None = None,
        agent_instance_id: int | None = None,
        before_id: int | None = None,
        limit: int = 20,
    ) -> tuple[tuple[MemoryItemDetail, ...], int | None]:
        if limit < 1 or limit > 50:
            raise ValueError("limit must be between 1 and 50")
        if before_id is not None and before_id <= 0:
            raise ValueError("before_id must be positive")
        resolved_scope: MemoryScope | None = None
        if scope is not None:
            resolved_scope = validate_memory_scope(scope)
            if resolved_scope == MemoryScope.AGENT and agent_instance_id is not None:
                self._agents.get_instance(owner_user_id, agent_instance_id)
        elif agent_instance_id is not None:
            self._agents.get_instance(owner_user_id, agent_instance_id)

        return self._persistence.list_memories(
            owner_user_id=owner_user_id,
            scope=resolved_scope,
            agent_instance_id=agent_instance_id,
            before_id=before_id,
            limit=limit,
        )

    def list_memory_versions(
        self,
        *,
        owner_user_id: int,
        memory_item_id: int,
        before_version: int | None = None,
        limit: int = 20,
    ) -> tuple[tuple[MemoryVersion, ...], int | None]:
        if limit < 1 or limit > 50:
            raise ValueError("limit must be between 1 and 50")
        if before_version is not None and before_version <= 0:
            raise ValueError("before_version must be positive")
        return self._persistence.list_memory_versions(
            owner_user_id=owner_user_id,
            memory_item_id=memory_item_id,
            before_version=before_version,
            limit=limit,
        )

    def edit_memory(
        self,
        *,
        owner_user_id: int,
        memory_item_id: int,
        expected_version: int,
        content: str,
    ) -> MemoryItemDetail:
        if expected_version <= 0:
            raise ValueError("expected_version must be positive")
        valid_content = validate_memory_content(content)
        digest = compute_memory_digest(valid_content)
        now = require_utc(self._clock())
        return self._persistence.edit_memory(
            owner_user_id=owner_user_id,
            memory_item_id=memory_item_id,
            expected_version=expected_version,
            content=valid_content,
            content_digest=digest,
            now=now,
        )

    def delete_memory(
        self,
        *,
        owner_user_id: int,
        memory_item_id: int,
        expected_version: int | None = None,
    ) -> None:
        if expected_version is not None and expected_version <= 0:
            raise ValueError("expected_version must be positive")
        now = require_utc(self._clock())
        self._persistence.delete_memory(
            owner_user_id=owner_user_id,
            memory_item_id=memory_item_id,
            expected_version=expected_version,
            now=now,
        )

    def retrieve_candidates(
        self, owner_user_id: int, agent_instance_id: int, limit: int = 50
    ) -> tuple[RetrievedMemoryItem, ...]:
        query = MemoryRetrievalQuery(
            owner_user_id=owner_user_id,
            agent_instance_id=agent_instance_id,
            candidate_limit=limit,
        )
        return self._persistence.retrieve_candidates(query)


__all__ = [
    "MemoryPersistence",
    "MemoryRetrievalPort",
    "MemoryRetrievalQuery",
    "MemoryService",
    "RetrievedMemoryItem",
]
