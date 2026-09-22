"""F2: Context assembly domain values, canonical rendering, and compaction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from nervos_core.domain.conversations import (
    MessageRole,
)

CONTEXT_SCHEMA_VERSION = 1
CONTEXT_BUILDER_VERSION = "nervos.context.v1"

MAX_CONTEXT_BYTES = 8000
MAX_CONTEXT_CODE_POINTS = 4000
MAX_RECENT_HISTORY_MESSAGES = 20  # up to 10 completed paired turns
MAX_COMPACTION_STORED_BYTES = 32000

COMPACTION_HEADER_STORED = "[Conversation Compaction v1]"
COMPACTION_WRAPPER_PREFIX = "[Earlier Conversation Context]\n"
COMPACTION_WRAPPER_SUFFIX = "\n\n"


@dataclass(frozen=True, slots=True)
class HistoricalMessage:
    turn_id: int
    sequence: int
    message_id: int
    role: MessageRole
    content: str


@dataclass(frozen=True, slots=True)
class CompactionData:
    version: int
    source_start_sequence: int
    source_end_sequence: int
    content: str
    content_digest: bytes


@dataclass(frozen=True, slots=True)
class ContextSnapshotData:
    run_id: int
    turn_id: int
    schema_version: int
    builder_version: str
    current_user_text: str
    history_messages: tuple[HistoricalMessage, ...]
    selected_turn_ids: tuple[int, ...]
    selected_message_ids: tuple[int, ...]
    compaction_version: int | None
    compaction_source_start: int | None
    compaction_source_end: int | None
    injected_compaction_text: str | None
    agent_key: str
    agent_definition_version: str
    max_total_bytes: int
    max_total_code_points: int
    actual_total_bytes: int
    actual_total_code_points: int
    rendered_context: str
    content_digest: bytes
    created_at: datetime


def format_stored_compaction_v1(
    turns: list[tuple[int, str, str]],
) -> str:
    """Format eligible turn pairs into the canonical stored compaction format.

    turns: list of (turn_sequence, user_content, assistant_content)
    """
    if not turns:
        return COMPACTION_HEADER_STORED

    blocks: list[str] = []
    for seq, user_text, assistant_text in turns:
        blocks.append(f"Turn {seq}:\nUser: {user_text}\nAssistant: {assistant_text}")

    body = "\n\n".join(blocks)
    return f"{COMPACTION_HEADER_STORED}\n{body}"


def render_context_v1(
    *,
    current_user_text: str,
    history_messages: tuple[HistoricalMessage, ...] = (),
    injected_compaction_text: str | None = None,
) -> str:
    """Render canonical assembled context for Run.input_text and Snapshot.rendered_context.

    This single canonical renderer guarantees that Run.input_text and Snapshot.rendered_context
    are always byte-for-byte identical.
    """
    parts: list[str] = []

    # 1. Injected compaction (if present)
    if injected_compaction_text:
        parts.append(f"{COMPACTION_WRAPPER_PREFIX}{injected_compaction_text}")

    # 2. Historical messages (if present)
    if history_messages:
        hist_lines: list[str] = []
        for msg in history_messages:
            role_label = "User" if msg.role == MessageRole.USER else "Assistant"
            hist_lines.append(f"Turn {msg.sequence} ({role_label}): {msg.content}")
        parts.append("\n\n".join(hist_lines))

    # 3. Current user message (always present)
    parts.append(current_user_text)

    return "\n\n".join(parts)


def compute_context_digest(rendered_context: str) -> bytes:
    """Compute exact SHA-256 digest of canonical UTF-8 rendered context bytes."""
    return hashlib.sha256(rendered_context.encode("utf-8")).digest()


def serialize_history_messages(messages: tuple[HistoricalMessage, ...]) -> str:
    """Serialize structured historical messages to canonical deterministic JSON."""
    data = [
        {
            "turn_id": msg.turn_id,
            "sequence": msg.sequence,
            "message_id": msg.message_id,
            "role": msg.role.value,
            "content": msg.content,
        }
        for msg in messages
    ]
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def deserialize_history_messages(raw_json: str) -> tuple[HistoricalMessage, ...]:
    """Deserialize structured historical messages from canonical JSON."""
    data: Any = json.loads(raw_json)
    if not isinstance(data, list):
        raise ValueError("history_messages JSON must be a list")
    messages: list[HistoricalMessage] = []
    for raw_item in cast("list[Any]", data):
        if not isinstance(raw_item, dict):
            raise ValueError("history_messages items must be dicts")
        item = cast("dict[str, Any]", raw_item)
        messages.append(
            HistoricalMessage(
                turn_id=int(item["turn_id"]),
                sequence=int(item["sequence"]),
                message_id=int(item["message_id"]),
                role=MessageRole(str(item["role"])),
                content=str(item["content"]),
            )
        )
    return tuple(messages)


def serialize_id_list(ids: tuple[int, ...]) -> str:
    """Serialize integer ID tuple to canonical JSON list."""
    return json.dumps(list(ids), sort_keys=True, separators=(",", ":"))


def deserialize_id_list(raw_json: str) -> tuple[int, ...]:
    """Deserialize integer ID tuple from JSON list."""
    data: Any = json.loads(raw_json)
    if not isinstance(data, list):
        raise ValueError("ID list must be a JSON array")
    return tuple(int(x) for x in cast("list[Any]", data))


__all__ = [
    "COMPACTION_HEADER_STORED",
    "COMPACTION_WRAPPER_PREFIX",
    "COMPACTION_WRAPPER_SUFFIX",
    "CONTEXT_BUILDER_VERSION",
    "CONTEXT_SCHEMA_VERSION",
    "MAX_COMPACTION_STORED_BYTES",
    "MAX_CONTEXT_BYTES",
    "MAX_CONTEXT_CODE_POINTS",
    "MAX_RECENT_HISTORY_MESSAGES",
    "CompactionData",
    "ContextSnapshotData",
    "HistoricalMessage",
    "compute_context_digest",
    "deserialize_history_messages",
    "deserialize_id_list",
    "format_stored_compaction_v1",
    "render_context_v1",
    "serialize_history_messages",
    "serialize_id_list",
]
