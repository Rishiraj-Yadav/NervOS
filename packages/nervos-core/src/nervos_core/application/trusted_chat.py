"""Trusted `nervos.chat@1` behavior: fixed versioned instruction and one-shot execution."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from nervos_core.application.model_completion import (
    MODEL_OUTPUT_INCOMPLETE,
    MODEL_REFUSED,
    MODEL_RESPONSE_INVALID,
    ModelCompletion,
    ModelOutputTooLargeError,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelResponseInvalidError,
    ModelUsage,
    StopOutcome,
)
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.runs import Run, RunStatus, is_blank_text

# Only a normalized natural stop is representable as a succeeded Run. Every other
# normalized termination becomes a stable safe execution failure with no output.
_STOP_OUTCOMES: dict[StopOutcome, str] = {
    StopOutcome.STOP: "stop",
}
_STOP_FAILURE_CODES: dict[StopOutcome, str] = {
    StopOutcome.INCOMPLETE: MODEL_OUTPUT_INCOMPLETE,
    StopOutcome.REFUSED: MODEL_REFUSED,
    StopOutcome.INVALID: MODEL_RESPONSE_INVALID,
}

CHAT_DEFINITION_ID = AgentDefinitionId("nervos.chat", "1")

# Fixed version-1 behavior. Any behaviorally meaningful change requires a new definition
# version rather than silently mutating this text.
NERVOS_CHAT_SYSTEM_INSTRUCTION = (
    "You are a helpful assistant. Answer the user's request directly and accurately."
)


class UnknownAgentHandler(LookupError):
    """Raised when no trusted handler exists for the exact definition identity."""


class DuplicateAgentHandler(ValueError):
    """Raised when a trusted handler is registered twice for one exact identity."""


@dataclass(frozen=True, slots=True)
class ChatOutcome:
    """Validated provider-neutral result of one trusted Chat execution."""

    output_text: str
    finish_reason: str
    usage: ModelUsage


class TrustedAgentHandler(Protocol):
    async def run(self, completion: ModelCompletion, run: Run, elapsed_ms: int) -> ChatOutcome: ...


class TrustedAgentHandlerResolver(Protocol):
    def resolve(self, definition_id: AgentDefinitionId) -> TrustedAgentHandler: ...


class NervosChatHandler:
    """One-shot Chat behavior for exactly the version-1 built-in definition."""

    async def run(self, completion: ModelCompletion, run: Run, elapsed_ms: int) -> ChatOutcome:
        del elapsed_ms  # Measured by the coordinator; part of the handler contract only.
        if run.status is not RunStatus.RUNNING:
            raise ModelResponseInvalidError
        if run.limits.max_model_calls != 1:
            raise ModelResponseInvalidError
        request = ModelRequest(
            NERVOS_CHAT_SYSTEM_INSTRUCTION,
            run.input_text,
            run.model_name,
            run.limits.max_output_tokens,
            run.limits.provider_timeout_ms,
        )
        response = await completion.complete(request)
        self._verify_identity(response, run)
        if response.finish_reason is None:
            raise ModelResponseInvalidError
        failure_code = _STOP_FAILURE_CODES.get(response.finish_reason)
        if failure_code is not None:
            raise ModelProviderError(failure_code, usage=response.usage)
        finish_reason = _STOP_OUTCOMES.get(response.finish_reason)
        if finish_reason is None:
            raise ModelResponseInvalidError
        output_text = self._validated_output(response.text, run)
        return ChatOutcome(output_text, finish_reason, response.usage or ModelUsage())

    @staticmethod
    def _verify_identity(response: ModelResponse, run: Run) -> None:
        if response.model_provider != run.model_provider:
            raise ModelResponseInvalidError
        if is_blank_text(response.model_name):
            raise ModelResponseInvalidError

    @staticmethod
    def _validated_output(text: str, run: Run) -> str:
        if is_blank_text(text) or "\x00" in text:
            raise ModelResponseInvalidError
        limits = run.limits
        if len(text) > limits.output_max_code_points or len(text.encode("utf-8")) > (
            limits.output_max_bytes
        ):
            raise ModelOutputTooLargeError
        return text


class BuiltInTrustedAgentHandlerRegistry:
    """Immutable exact-version handler registry."""

    def __init__(self, handlers: Iterable[tuple[AgentDefinitionId, TrustedAgentHandler]]) -> None:
        entries: dict[AgentDefinitionId, TrustedAgentHandler] = {}
        for identity, handler in handlers:
            if identity in entries:
                raise DuplicateAgentHandler(identity)
            entries[identity] = handler
        self._entries = entries

    def resolve(self, definition_id: AgentDefinitionId) -> TrustedAgentHandler:
        handler = self._entries.get(definition_id)
        if handler is None:
            raise UnknownAgentHandler(definition_id)
        return handler


def create_builtin_handler_registry() -> TrustedAgentHandlerResolver:
    """Return the immutable built-in trusted handler registry."""
    return BuiltInTrustedAgentHandlerRegistry([(CHAT_DEFINITION_ID, NervosChatHandler())])
