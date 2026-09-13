"""Narrow provider-neutral model completion boundary for later B2 use."""

from dataclasses import dataclass
from typing import Protocol

from nervos_core.domain.runs import ModelUsage


@dataclass(frozen=True, slots=True)
class ModelRequest:
    system_instruction: str
    user_text: str
    model_name: str
    max_output_tokens: int
    timeout_ms: int


@dataclass(frozen=True, slots=True)
class ModelResponse:
    text: str
    model_provider: str
    model_name: str
    finish_reason: str | None = None
    usage: ModelUsage | None = None


class ModelCompletion(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...
