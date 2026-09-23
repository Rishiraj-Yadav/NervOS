"""F3 Scoped Memory domain models, enums, bounds, and validation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from nervos_core.domain.conversations import NERVOS_BLANK_TEXT_CODE_POINTS

MAX_MEMORY_CONTENT_BYTES = 32000
MAX_ACTIVE_MEMORY_ITEMS_PER_SCOPE = 1000


def is_blank_text(value: str) -> bool:
    return not value or all(character in NERVOS_BLANK_TEXT_CODE_POINTS for character in value)


class InvalidMemory(ValueError):
    """Raised when a Memory entity or payload violates validation rules."""


class InvalidMemorySource(ValueError):
    """Raised when a promotion source is invalid, unpromotable, or missing."""


class MemoryScopeCapacityExceeded(Exception):
    """Raised when a scope has reached the 1000 active items limit."""


class MemoryNotFound(LookupError):
    """Raised when a Memory item is not found or belongs to another owner."""


class MemoryScope(StrEnum):
    USER = "user"
    AGENT = "agent"


class MemorySourceKind(StrEnum):
    DIRECT_USER = "direct_user"
    PROMOTED_MESSAGE = "promoted_message"
    PROMOTED_RUN = "promoted_run"


class MemoryProvenanceType(StrEnum):
    USER_AUTHORED = "user_authored"
    USER_APPROVED_INFERRED = "user_approved_inferred"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    DELETED = "deleted"


def validate_memory_content(content: str) -> str:
    """Validate memory content string according to frozen 32000-byte authority."""
    if not content:
        raise InvalidMemory("memory content must not be empty")
    if is_blank_text(content):
        raise InvalidMemory("memory content must not be blank")
    if "\x00" in content:
        raise InvalidMemory("memory content must not contain NUL bytes")
    if len(content.encode("utf-8")) > MAX_MEMORY_CONTENT_BYTES:
        raise InvalidMemory(
            f"memory content exceeds maximum {MAX_MEMORY_CONTENT_BYTES} UTF-8 bytes"
        )
    return content


def validate_memory_scope(scope: str) -> MemoryScope:
    try:
        return MemoryScope(scope)
    except ValueError as err:
        raise InvalidMemory(f"invalid MemoryScope: {scope}") from err


def compute_memory_digest(content: str) -> bytes:
    """Compute exact SHA-256 digest of validated UTF-8 memory content bytes."""
    return hashlib.sha256(content.encode("utf-8")).digest()


@dataclass(frozen=True, slots=True)
class MemoryItem:
    id: int
    owner_user_id: int
    agent_instance_id: int | None
    scope: MemoryScope
    status: MemoryStatus
    current_version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class MemoryVersion:
    id: int
    memory_item_id: int
    version: int
    content: str
    content_digest: bytes
    source_kind: MemorySourceKind
    source_id: int | None
    provenance_type: MemoryProvenanceType
    created_by_user_id: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MemoryItemDetail:
    item: MemoryItem
    current_version_record: MemoryVersion


__all__ = [
    "MAX_ACTIVE_MEMORY_ITEMS_PER_SCOPE",
    "MAX_MEMORY_CONTENT_BYTES",
    "InvalidMemory",
    "InvalidMemorySource",
    "MemoryItem",
    "MemoryItemDetail",
    "MemoryNotFound",
    "MemoryProvenanceType",
    "MemoryScope",
    "MemoryScopeCapacityExceeded",
    "MemorySourceKind",
    "MemoryStatus",
    "MemoryVersion",
    "compute_memory_digest",
    "is_blank_text",
    "validate_memory_content",
    "validate_memory_scope",
]
