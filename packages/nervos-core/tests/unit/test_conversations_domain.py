"""Unit tests for F1 Conversation domain validation."""

from __future__ import annotations

import pytest
from nervos_core.domain.conversations import (
    CONVERSATION_TITLE_MAX_CODE_POINTS,
    MAX_CLIENT_MESSAGE_ID_LENGTH,
    InvalidConversation,
    MessageRole,
    RunLinkRole,
    TurnState,
    compute_content_digest,
    validate_client_message_id,
    validate_content_digest,
    validate_conversation_title,
    validate_message_content,
    validate_message_role,
    validate_run_link_role,
    validate_turn_sequence,
    validate_turn_state,
)


def test_title_validation() -> None:
    assert validate_conversation_title(None) is None
    assert validate_conversation_title("Valid Title") == "Valid Title"
    assert (
        validate_conversation_title("a" * CONVERSATION_TITLE_MAX_CODE_POINTS)
        == "a" * CONVERSATION_TITLE_MAX_CODE_POINTS
    )

    with pytest.raises(InvalidConversation, match="must not be blank"):
        validate_conversation_title("")
    with pytest.raises(InvalidConversation, match="must not be blank"):
        validate_conversation_title("   \t\n")
    with pytest.raises(InvalidConversation, match="exceeds maximum"):
        validate_conversation_title("a" * (CONVERSATION_TITLE_MAX_CODE_POINTS + 1))
    with pytest.raises(InvalidConversation, match="NUL"):
        validate_conversation_title("Title\x00with NUL")


def test_client_message_id_validation() -> None:
    assert validate_client_message_id("msg-12345") == "msg-12345"
    assert (
        validate_client_message_id("a" * MAX_CLIENT_MESSAGE_ID_LENGTH)
        == "a" * MAX_CLIENT_MESSAGE_ID_LENGTH
    )

    with pytest.raises(InvalidConversation, match="must be between 1 and"):
        validate_client_message_id("")
    with pytest.raises(InvalidConversation, match="must be between 1 and"):
        validate_client_message_id("a" * (MAX_CLIENT_MESSAGE_ID_LENGTH + 1))
    with pytest.raises(InvalidConversation, match="NUL"):
        validate_client_message_id("id\x00nul")


def test_content_digest() -> None:
    text = "Hello, world!"
    digest = compute_content_digest(text)
    assert len(digest) == 32
    assert validate_content_digest(digest) == digest

    with pytest.raises(InvalidConversation, match="32 bytes"):
        validate_content_digest(b"short")


def test_turn_state_validation() -> None:
    for state in ("pending", "running", "succeeded", "failed", "cancelled", "ambiguous"):
        assert validate_turn_state(state) == TurnState(state)

    with pytest.raises(InvalidConversation, match="invalid TurnState"):
        validate_turn_state("invalid")


def test_message_role_validation() -> None:
    assert validate_message_role("user") == MessageRole.USER
    assert validate_message_role("assistant") == MessageRole.ASSISTANT

    with pytest.raises(InvalidConversation, match="invalid MessageRole"):
        validate_message_role("system")


def test_run_link_role_validation() -> None:
    assert validate_run_link_role("initial") == RunLinkRole.INITIAL
    assert validate_run_link_role("retry") == RunLinkRole.RETRY

    with pytest.raises(InvalidConversation, match="invalid RunLinkRole"):
        validate_run_link_role("invalid")


def test_turn_sequence_validation() -> None:
    assert validate_turn_sequence(1) == 1
    assert validate_turn_sequence(42) == 42

    with pytest.raises(InvalidConversation, match="turn sequence must be positive"):
        validate_turn_sequence(0)
    with pytest.raises(InvalidConversation, match="turn sequence must be positive"):
        validate_turn_sequence(-1)


def test_message_content_validation() -> None:
    assert validate_message_content("Some valid content") == "Some valid content"

    with pytest.raises(InvalidConversation, match="must not be blank"):
        validate_message_content("")
    with pytest.raises(InvalidConversation, match="must not be blank"):
        validate_message_content("   \n")
    with pytest.raises(InvalidConversation, match="NUL"):
        validate_message_content("content\x00with NUL")
    with pytest.raises(InvalidConversation, match="exceeds output bounds"):
        validate_message_content("a" * 32001)
