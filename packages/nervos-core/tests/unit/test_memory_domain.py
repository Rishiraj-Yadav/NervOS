"""Unit tests for F3 Memory domain validation and models."""

from __future__ import annotations

import pytest
from nervos_core.domain.memory import (
    MAX_MEMORY_CONTENT_BYTES,
    InvalidMemory,
    MemoryProvenanceType,
    MemoryScope,
    MemorySourceKind,
    MemoryStatus,
    compute_memory_digest,
    validate_memory_content,
    validate_memory_scope,
)


def test_memory_content_validation() -> None:
    assert validate_memory_content("User prefers Python") == "User prefers Python"
    assert validate_memory_content("a" * MAX_MEMORY_CONTENT_BYTES) == "a" * MAX_MEMORY_CONTENT_BYTES

    with pytest.raises(InvalidMemory, match="must not be empty"):
        validate_memory_content("")
    with pytest.raises(InvalidMemory, match="must not be blank"):
        validate_memory_content("   \n\t")
    with pytest.raises(InvalidMemory, match="NUL"):
        validate_memory_content("content\x00with NUL")
    with pytest.raises(InvalidMemory, match="exceeds maximum"):
        validate_memory_content("a" * (MAX_MEMORY_CONTENT_BYTES + 1))


def test_memory_scope_validation() -> None:
    assert validate_memory_scope("user") == MemoryScope.USER
    assert validate_memory_scope("agent") == MemoryScope.AGENT

    with pytest.raises(InvalidMemory, match="invalid MemoryScope"):
        validate_memory_scope("workspace")
    with pytest.raises(InvalidMemory, match="invalid MemoryScope"):
        validate_memory_scope("conversation")


def test_compute_memory_digest() -> None:
    text = "User prefers SQLite for local persistence."
    digest = compute_memory_digest(text)
    assert len(digest) == 32
    assert isinstance(digest, bytes)


def test_memory_enums() -> None:
    assert MemorySourceKind.DIRECT_USER == "direct_user"
    assert MemorySourceKind.PROMOTED_MESSAGE == "promoted_message"
    assert MemorySourceKind.PROMOTED_RUN == "promoted_run"

    assert MemoryProvenanceType.USER_AUTHORED == "user_authored"
    assert MemoryProvenanceType.USER_APPROVED_INFERRED == "user_approved_inferred"

    assert MemoryStatus.ACTIVE == "active"
    assert MemoryStatus.DELETED == "deleted"


def test_stale_memory_version_exception() -> None:
    from nervos_core.domain.memory import StaleMemoryVersion

    err = StaleMemoryVersion("Version mismatch: expected 1, got 2")
    assert "Version mismatch" in str(err)


def test_conversation_status_validation() -> None:
    from nervos_core.domain.conversations import (
        ConversationStatus,
        InvalidConversation,
        validate_conversation_status,
    )

    assert validate_conversation_status("active") == ConversationStatus.ACTIVE
    assert validate_conversation_status("archived") == ConversationStatus.ARCHIVED
    assert validate_conversation_status("deleted") == ConversationStatus.DELETED

    with pytest.raises(InvalidConversation, match="invalid ConversationStatus"):
        validate_conversation_status("unknown")
