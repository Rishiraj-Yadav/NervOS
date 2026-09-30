"""The shared Stage-D authority for executing one already-resolved tool request."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from nervos_core.application.model_completion import (
    EXECUTION_CANCELLED,
    INTERNAL_EXECUTION_ERROR,
    TOOL_OUTCOME_UNKNOWN,
    ModelProviderError,
    ModelUsage,
    safe_error_message,
)
from nervos_core.application.tool_invocations import (
    ClaimHandle,
    InvocationRequest,
    RecordOutcomeKind,
    RefusalOutcomeKind,
    StartOutcomeKind,
    ToolInvocationPersistence,
    result_envelope,
)
from nervos_core.application.tool_permissions import ToolPermissionEvaluator
from nervos_core.application.tool_registry import (
    ToolExecutionFailure,
    ToolOutcomeUnknown,
    ToolRegistry,
    ToolResult,
)
from nervos_core.application.tool_schema import (
    SchemaRejection,
    validate_canonical_schema,
    validate_instance,
)
from nervos_core.domain.runs import Run
from nervos_core.domain.tools import JsonValue, ToolDescriptor, canonical_json_text

MAX_DURABLE_ERROR_MESSAGE_CHARS = 512
STRUCTURED_OMITTED_NOTE = "\n[NervOS: structured result omitted to fit this run's result limit.]"
TEXT_TRUNCATED_NOTE = "\n[NervOS: tool result truncated to fit this run's result limit.]"


@dataclass(frozen=True, slots=True)
class Observation:
    """A result payload after applying the Run's provider-neutral byte limit."""

    text: str
    structured: JsonValue | None
    is_error: bool
    truncated: bool


def bounded_observation(
    *,
    text: str,
    structured: JsonValue | None,
    is_error: bool,
    max_bytes: int,
) -> Observation:
    """Reduce one tool result to the Run's bounded observation contract."""
    if _observation_bytes(text, structured) <= max_bytes:
        return Observation(text, structured, is_error, truncated=False)
    if structured is not None:
        omitted = text + STRUCTURED_OMITTED_NOTE
        if _observation_bytes(omitted, None) <= max_bytes:
            return Observation(omitted, None, is_error, truncated=True)
    budget = max_bytes - len(TEXT_TRUNCATED_NOTE.encode("utf-8"))
    return Observation(
        _utf8_head(text, budget) + TEXT_TRUNCATED_NOTE,
        None,
        is_error,
        truncated=True,
    )


def _observation_bytes(text: str, structured: JsonValue | None) -> int:
    size = len(text.encode("utf-8"))
    if structured is not None:
        size += 1 + len(canonical_json_text(structured).encode("utf-8"))
    return size


def _utf8_head(text: str, budget: int) -> str:
    if budget <= 0:
        return ""
    return text.encode("utf-8")[:budget].decode("utf-8", errors="ignore")


def _durable_error_message(error_code: str, message: str | None) -> str:
    if message is not None and 1 <= len(message) <= MAX_DURABLE_ERROR_MESSAGE_CHARS:
        return message
    return safe_error_message(error_code)


@dataclass(frozen=True, slots=True)
class ToolInvocationOutcome:
    """A completed invocation or a safe denial for its caller to observe."""

    result: ToolResult | None
    denied: bool = False
    failed: bool = False
    invalid_arguments: bool = False


class ToolInvocationMediator:
    """Own live permission, durable audit, timeout, and dispatch for one tool call."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        authorize: ToolPermissionEvaluator,
        invocations: ToolInvocationPersistence,
        clock: Callable[[], datetime],
    ) -> None:
        self._registry = registry
        self._authorize = authorize
        self._invocations = invocations
        self._clock = clock

    async def invoke(
        self,
        *,
        descriptor: ToolDescriptor,
        arguments: Mapping[str, JsonValue],
        run: Run,
        claim: ClaimHandle,
        tool_sequence: int,
        provider_call_id: str,
        usage: ModelUsage | None = None,
    ) -> ToolInvocationOutcome:
        """Execute exactly one call through the Stage-D grant and audit boundaries."""
        usage = usage or ModelUsage()
        schema = validate_canonical_schema(descriptor.input_schema)
        if isinstance(schema, SchemaRejection) or validate_instance(schema, arguments) is not None:
            self.record_pre_dispatch_refusal(claim=claim, usage=usage)
            return ToolInvocationOutcome(None, invalid_arguments=True)
        decision = self._authorize.check_permission(
            run_id=run.id, tool_definition_id=descriptor.tool_definition_id
        )
        requested = self._invocations.record_requested(
            claim=claim,
            request=InvocationRequest(
                run_id=run.id,
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                tool_sequence=tool_sequence,
                tool_definition_id=descriptor.tool_definition_id,
                source_kind=descriptor.source_kind,
                source_id=descriptor.source_id,
                upstream_name=descriptor.upstream_name,
                model_name=descriptor.model_name,
                definition_fingerprint=descriptor.fingerprint,
                provider_call_id=provider_call_id,
                permission_decision=decision,
                arguments=arguments,
            ),
            now=self._clock(),
        )
        if requested.kind is RecordOutcomeKind.FENCED or requested.invocation_id is None:
            raise ModelProviderError(INTERNAL_EXECUTION_ERROR, usage=usage)
        invocation_id = requested.invocation_id
        if not decision.allowed:
            self._invocations.mark_denied(
                claim=claim,
                invocation_id=invocation_id,
                decision=decision,
                now=self._clock(),
            )
            return ToolInvocationOutcome(None, denied=True)

        started = self._invocations.mark_started(
            claim=claim, invocation_id=invocation_id, now=self._clock()
        )
        if started.kind is StartOutcomeKind.FENCED:
            raise ModelProviderError(INTERNAL_EXECUTION_ERROR, usage=usage)
        if started.kind is StartOutcomeKind.CANCELLED:
            raise ModelProviderError(EXECUTION_CANCELLED, usage=usage)
        if started.kind is StartOutcomeKind.DENIED:
            return ToolInvocationOutcome(None, denied=True)

        try:
            async with asyncio.timeout(run.limits.tool_timeout_ms / 1000):
                result = await self._registry.executor(descriptor.source_ref).execute(
                    descriptor, arguments
                )
        except TimeoutError:
            self._mark_ambiguous(claim, invocation_id)
            raise ModelProviderError(TOOL_OUTCOME_UNKNOWN, usage=usage) from None
        except ToolExecutionFailure as failure:
            if not self._invocations.mark_failed(
                claim=claim,
                invocation_id=invocation_id,
                error_code=failure.reason.value,
                error_message=_durable_error_message(failure.reason.value, failure.message),
                now=self._clock(),
            ):
                raise ModelProviderError(INTERNAL_EXECUTION_ERROR, usage=usage) from None
            return ToolInvocationOutcome(
                self._bounded_result(ToolResult(failure.message), run), failed=True
            )
        except ToolOutcomeUnknown:
            self._mark_ambiguous(claim, invocation_id)
            raise ModelProviderError(TOOL_OUTCOME_UNKNOWN, usage=usage) from None
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self._invocations.mark_failed(
                claim=claim,
                invocation_id=invocation_id,
                error_code=INTERNAL_EXECUTION_ERROR,
                error_message=safe_error_message(INTERNAL_EXECUTION_ERROR),
                now=self._clock(),
            ):
                raise ModelProviderError(INTERNAL_EXECUTION_ERROR, usage=usage) from None
            raise ModelProviderError(INTERNAL_EXECUTION_ERROR, usage=usage) from None

        if not self._invocations.mark_succeeded(
            claim=claim,
            invocation_id=invocation_id,
            envelope=result_envelope(text=result.text, structured=result.structured),
            now=self._clock(),
        ):
            raise ModelProviderError(INTERNAL_EXECUTION_ERROR, usage=usage)
        return ToolInvocationOutcome(self._bounded_result(result, run))

    @staticmethod
    def _bounded_result(result: ToolResult, run: Run) -> ToolResult:
        observation = bounded_observation(
            text=result.text,
            structured=result.structured,
            is_error=False,
            max_bytes=run.limits.tool_result_max_bytes,
        )
        return ToolResult(observation.text, observation.structured)

    def record_pre_dispatch_refusal(
        self, *, claim: ClaimHandle, usage: ModelUsage | None = None
    ) -> None:
        """Persist one safe refusal that cannot be resolved to an executable descriptor."""
        refusal = self._invocations.record_pre_dispatch_refusal(claim=claim, now=self._clock())
        if refusal.kind is RefusalOutcomeKind.CANCELLED:
            raise ModelProviderError(EXECUTION_CANCELLED, usage=usage)
        if refusal.kind is RefusalOutcomeKind.FENCED:
            raise ModelProviderError(INTERNAL_EXECUTION_ERROR, usage=usage)

    def _mark_ambiguous(self, claim: ClaimHandle, invocation_id: int) -> None:
        self._invocations.mark_ambiguous(
            claim=claim,
            invocation_id=invocation_id,
            error_code=TOOL_OUTCOME_UNKNOWN,
            error_message=safe_error_message(TOOL_OUTCOME_UNKNOWN),
            now=self._clock(),
        )
