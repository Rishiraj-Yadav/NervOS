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
from nervos_core.domain.context import ContextSnapshotData
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
# The tool-enabled version of the same agent. It is a new exact identity rather than a mutation of
# chat@1, because "this agent may now call tools" is a behaviourally meaningful change to what a Run
# of this definition can do, and a version is how Stage B already freezes such a change.
CHAT_TOOL_DEFINITION_ID = AgentDefinitionId("nervos.chat", "2")

# Fixed version-1 behavior. Any behaviorally meaningful change requires a new definition
# version rather than silently mutating this text.
NERVOS_CHAT_SYSTEM_INSTRUCTION = (
    "You are a helpful assistant. Answer the user's request directly and accurately."
)
# Fixed version-2 behavior, reviewed as part of the D4 authorization.
NERVOS_TOOL_CHAT_SYSTEM_INSTRUCTION = (
    "You are a helpful assistant. You may call provided tools to answer accurately. "
    "Use a tool only when it is needed, and answer in your own words once you have what you need."
)


def verified_identity(response: ModelResponse, run: Run) -> None:
    """Fail closed unless the response came from the provider and model this Run snapshotted."""
    if response.model_provider != run.model_provider:
        raise ModelResponseInvalidError
    if is_blank_text(response.model_name):
        raise ModelResponseInvalidError


def validated_final_output(text: str, run: Run) -> str:
    """Return the accepted final answer, or fail closed.

    Shared by both trusted Chat definitions: a final answer is a final answer whether or not the
    Run was allowed to use tools.
    """
    if is_blank_text(text) or "\x00" in text:
        raise ModelResponseInvalidError
    limits = run.limits
    if len(text) > limits.output_max_code_points or len(text.encode("utf-8")) > (
        limits.output_max_bytes
    ):
        raise ModelOutputTooLargeError
    return text


def final_chat_outcome(response: ModelResponse, run: Run, usage: ModelUsage | None) -> ChatOutcome:
    """Validate one concluding model turn into the Run's final answer, or fail closed.

    The single place that decides what a final turn may be: the provider and model must match the
    Run's snapshot, the turn must carry no tool calls, and its termination must be a real
    conclusion. Every other normalized termination becomes a stable safe failure with no output.
    """
    verified_identity(response, run)
    if response.tool_calls:
        raise ModelResponseInvalidError
    if response.finish_reason is None:
        raise ModelResponseInvalidError
    failure_code = _STOP_FAILURE_CODES.get(response.finish_reason)
    if failure_code is not None:
        raise ModelProviderError(failure_code, usage=usage)
    finish_reason = _STOP_OUTCOMES.get(response.finish_reason)
    if finish_reason is None:
        raise ModelResponseInvalidError
    return ChatOutcome(
        validated_final_output(response.text, run), finish_reason, usage or ModelUsage()
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
    async def run(
        self,
        completion: ModelCompletion,
        run: Run,
        elapsed_ms: int,
        snapshot: ContextSnapshotData | None = None,
    ) -> ChatOutcome: ...


class TrustedAgentHandlerResolver(Protocol):
    def resolve(self, definition_id: AgentDefinitionId) -> TrustedAgentHandler: ...


class NervosChatHandler:
    """One-shot Chat behavior for exactly the version-1 built-in definition."""

    async def run(
        self,
        completion: ModelCompletion,
        run: Run,
        elapsed_ms: int,
        snapshot: ContextSnapshotData | None = None,
    ) -> ChatOutcome:
        del elapsed_ms  # Measured by the coordinator; part of the handler contract only.
        if run.status is not RunStatus.RUNNING:
            raise ModelResponseInvalidError
        if run.limits.max_model_calls != 1:
            raise ModelResponseInvalidError
        user_text = snapshot.current_user_text if snapshot is not None else run.input_text
        history = snapshot.history_messages if snapshot is not None else ()
        compaction = snapshot.injected_compaction_text if snapshot is not None else None
        user_mem = snapshot.injected_user_memory_text if snapshot is not None else None
        agent_mem = snapshot.injected_agent_memory_text if snapshot is not None else None
        request = ModelRequest(
            system_instruction=NERVOS_CHAT_SYSTEM_INSTRUCTION,
            user_text=user_text,
            model_name=run.model_name,
            max_output_tokens=run.limits.max_output_tokens,
            timeout_ms=run.limits.provider_timeout_ms,
            history=history,
            compaction_context=compaction,
            user_memory_context=user_mem,
            agent_memory_context=agent_mem,
        )
        response = await completion.complete(request)
        return final_chat_outcome(response, run, response.usage)


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
