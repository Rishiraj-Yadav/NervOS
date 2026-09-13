"""Trusted Agent Definition and Agent Instance domain values."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime

from nervos_core.domain.runs import RunLimits

AGENT_KEY_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:\.[a-z0-9]+)+\Z")
VERSION_PATTERN = re.compile(r"[!-~]{1,64}\Z")
PROVIDER_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")
MAX_AGENT_KEY_LENGTH = 128
MAX_DISPLAY_NAME_CODE_POINTS = 100
MAX_DISPLAY_NAME_BYTES = 400
MAX_MODEL_NAME_CODE_POINTS = 256
MAX_MODEL_NAME_BYTES = 1024


class InvalidAgentDefinitionId(ValueError):
    """Raised when an exact definition identity is invalid."""


class InvalidAgentInstance(ValueError):
    """Raised when Agent Instance configuration is invalid."""


def _has_control(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidAgentInstance("timestamp must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class AgentDefinitionId:
    """Exact, versioned trusted Agent Definition identity."""

    agent_key: str
    agent_definition_version: str

    def __post_init__(self) -> None:
        if (
            len(self.agent_key) > MAX_AGENT_KEY_LENGTH
            or AGENT_KEY_PATTERN.fullmatch(self.agent_key) is None
            or VERSION_PATTERN.fullmatch(self.agent_definition_version) is None
        ):
            raise InvalidAgentDefinitionId


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """Generic immutable definition metadata required by B1."""

    identity: AgentDefinitionId
    display_name: str
    limits: RunLimits


@dataclass(frozen=True, slots=True)
class AgentInstance:
    """Explicit user-owned configuration pinned to one definition version."""

    id: int
    owner_user_id: int
    definition_id: AgentDefinitionId
    display_name: str
    enabled: bool
    model_provider: str
    model_name: str
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.id <= 0 or self.owner_user_id <= 0 or type(self.enabled) is not bool:
            raise InvalidAgentInstance
        validate_display_name(self.display_name, normalized=True)
        validate_model_provider(self.model_provider)
        validate_model_name(self.model_name, normalized=True)
        created_at = _require_utc(self.created_at)
        updated_at = _require_utc(self.updated_at)
        if updated_at < created_at:
            raise InvalidAgentInstance("updated_at precedes created_at")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)


def validate_display_name(value: str, *, normalized: bool = False) -> str:
    result = value if normalized else value.strip()
    if (
        not result
        or result != result.strip()
        or len(result) > MAX_DISPLAY_NAME_CODE_POINTS
        or len(result.encode("utf-8")) > MAX_DISPLAY_NAME_BYTES
        or _has_control(result)
    ):
        raise InvalidAgentInstance("invalid display name")
    return result


def validate_model_provider(value: str) -> str:
    if len(value) > 64 or PROVIDER_PATTERN.fullmatch(value) is None:
        raise InvalidAgentInstance("invalid model provider")
    return value


def validate_model_name(value: str, *, normalized: bool = False) -> str:
    result = value if normalized else value.strip()
    if (
        not result
        or result != result.strip()
        or len(result) > MAX_MODEL_NAME_CODE_POINTS
        or len(result.encode("utf-8")) > MAX_MODEL_NAME_BYTES
        or _has_control(result)
    ):
        raise InvalidAgentInstance("invalid model name")
    return result
