"""F1: Conversation domain values and lifecycle validation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

MAX_CLIENT_MESSAGE_ID_LENGTH = 128
CONVERSATION_TITLE_MAX_CODE_POINTS = 100
CONVERSATION_TITLE_MAX_BYTES = 400

NERVOS_BLANK_TEXT_CODE_POINTS = frozenset(
    {
        chr(0x0009),
        chr(0x000A),
        chr(0x000B),
        chr(0x000C),
        chr(0x000D),
        chr(0x0020),
        chr(0x0085),
        chr(0x00A0),
        chr(0x1680),
        chr(0x2000),
        chr(0x2001),
        chr(0x2002),
        chr(0x2003),
        chr(0x2004),
        chr(0x2005),
        chr(0x2006),
        chr(0x2007),
        chr(0x2008),
        chr(0x2009),
        chr(0x200A),
        chr(0x2028),
        chr(0x2029),
        chr(0x202F),
        chr(0x205F),
        chr(0x3000),
    }
)


def is_blank_text(value: str) -> bool:
    return not value or all(character in NERVOS_BLANK_TEXT_CODE_POINTS for character in value)


def validate_client_message_id(value: str) -> str:
    if not value or len(value) > MAX_CLIENT_MESSAGE_ID_LENGTH:
        raise InvalidConversation(
            f"client_message_id must be between 1 and {MAX_CLIENT_MESSAGE_ID_LENGTH} characters"
        )
    if "\x00" in value:
        raise InvalidConversation("client_message_id must not contain NUL")
    return value


def validate_conversation_title(value: str | None) -> str | None:
    if value is None:
        return None
    if is_blank_text(value):
        raise InvalidConversation("conversation title must not be blank")
    if (
        len(value) > CONVERSATION_TITLE_MAX_CODE_POINTS
        or len(value.encode("utf-8")) > CONVERSATION_TITLE_MAX_BYTES
    ):
        raise InvalidConversation("conversation title exceeds maximum 100 code points / 400 bytes")
    if "\x00" in value:
        raise InvalidConversation("conversation title must not contain NUL")
    return value


def compute_content_digest(content: str) -> bytes:
    """Compute SHA-256 digest of exact validated UTF-8 content bytes.

    No trimming, Unicode normalization, case folding, or whitespace normalization
    is performed. The exact UTF-8 bytes of the already-validated content are hashed.
    """
    return hashlib.sha256(content.encode("utf-8")).digest()


def validate_content_digest(value: bytes) -> bytes:
    if len(value) != 32:
        raise InvalidConversation("content_digest must be exactly 32 bytes (SHA-256)")
    return value


class InvalidConversation(ValueError):
    """Raised when a Conversation entity violates validation rules."""


class TurnState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    AMBIGUOUS = "ambiguous"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class RunLinkRole(StrEnum):
    INITIAL = "initial"
    RETRY = "retry"


def validate_turn_state(value: str) -> TurnState:
    try:
        return TurnState(value)
    except ValueError as err:
        raise InvalidConversation(f"invalid TurnState: {value}") from err


def validate_message_role(value: str) -> MessageRole:
    try:
        return MessageRole(value)
    except ValueError as err:
        raise InvalidConversation(f"invalid MessageRole: {value}") from err


def validate_run_link_role(value: str) -> RunLinkRole:
    try:
        return RunLinkRole(value)
    except ValueError as err:
        raise InvalidConversation(f"invalid RunLinkRole: {value}") from err


def validate_turn_sequence(value: int) -> int:
    if value <= 0:
        raise InvalidConversation("turn sequence must be positive")
    return value


def validate_message_content(value: str) -> str:
    if is_blank_text(value):
        raise InvalidConversation("message content must not be blank")
    if len(value.encode("utf-8")) > 32000 or len(value) > 16000:
        raise InvalidConversation("message content exceeds output bounds")
    if "\x00" in value:
        raise InvalidConversation("message content must not contain NUL")
    return value


@dataclass(frozen=True, slots=True)
class Conversation:
    id: int
    owner_user_id: int
    agent_instance_id: int
    title: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    id: int
    conversation_id: int
    sequence: int
    state: TurnState
    client_message_id: str
    content_digest: bytes
    authoritative_run_id: int | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    id: int
    turn_id: int
    role: MessageRole
    content: str
    source_run_id: int | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ConversationRunLink:
    id: int
    turn_id: int
    run_id: int
    ordinal: int
    role: RunLinkRole
    created_at: datetime


__all__ = [
    "CONVERSATION_TITLE_MAX_BYTES",
    "CONVERSATION_TITLE_MAX_CODE_POINTS",
    "MAX_CLIENT_MESSAGE_ID_LENGTH",
    "Conversation",
    "ConversationMessage",
    "ConversationRunLink",
    "ConversationTurn",
    "InvalidConversation",
    "MessageRole",
    "RunLinkRole",
    "TurnState",
    "compute_content_digest",
    "validate_client_message_id",
    "validate_content_digest",
    "validate_conversation_title",
    "validate_message_content",
    "validate_message_role",
    "validate_run_link_role",
    "validate_turn_sequence",
    "validate_turn_state",
]
