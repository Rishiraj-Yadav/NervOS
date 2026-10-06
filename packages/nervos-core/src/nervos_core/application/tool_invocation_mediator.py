"""The shared Stage-D authority for executing one already-resolved tool request."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from nervos_core.application.account_actions import AccountActionDispatcher
from nervos_core.application.approvals import (
    ApprovalAdmission,
    ApprovalGate,
    ApprovalRequest,
    requires_approval,
)
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
from nervos_core.application.tool_permissions import (
    PermissionDecision,
    PermissionDenialReason,
    ToolPermissionEvaluator,
)
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


def _approval_refused() -> PermissionDecision:
    """The one durable decision value an unmet approval produces.

    ADR 0015 reserved `HARD_POLICY_DENIED` for runtime policy that is not a grant question,
    which is exactly what an unmet approval is. Keeping it a single factory means every
    approval refusal is recorded with the same vocabulary, and none of them invents a new one.
    """
    return PermissionDecision(allowed=False, reason=PermissionDenialReason.HARD_POLICY_DENIED)


@dataclass(frozen=True, slots=True)
class _GatedAction:
    """One approval-required action together with the gate that owns its authority.

    Carrying both in a single optional value keeps the dispatch-time re-check free of a second
    lookup and free of an assertion: if this value exists, the gate exists.
    """

    gate: ApprovalGate
    request: ApprovalRequest


@dataclass(frozen=True, slots=True)
class ToolInvocationOutcome:
    """A completed invocation or a safe denial for its caller to observe."""

    result: ToolResult | None
    denied: bool = False
    failed: bool = False
    invalid_arguments: bool = False


class ToolInvocationMediator:
    """Own live permission, durable audit, timeout, and dispatch for one tool call.

    ``approvals`` is the Stage-H3 gate (ADR 0034). It is a constructor argument rather than a
    global so the Worker composes it and every other caller keeps the unchanged Stage-D path: an
    un-gated mediator does not silently gain an authority predicate, and a gated one cannot be
    built by accident.
    """

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        authorize: ToolPermissionEvaluator,
        invocations: ToolInvocationPersistence,
        clock: Callable[[], datetime],
        approvals: ApprovalGate | None = None,
        account_actions: AccountActionDispatcher | None = None,
    ) -> None:
        self._registry = registry
        self._authorize = authorize
        self._invocations = invocations
        self._clock = clock
        self._approvals = approvals
        self._account_actions = account_actions

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

        # The approval gate runs only for an action D2 has already allowed: an approval
        # request for a call the owner never granted would put a durable row in front of
        # the owner for something they would only ever be able to deny.
        gated: _GatedAction | None = None
        if self._approvals is not None and requires_approval(descriptor):
            gated = _GatedAction(
                gate=self._approvals,
                request=ApprovalRequest(
                    agent_instance_id=run.agent_instance_id,
                    run_id=run.id,
                    job_id=claim.job_id,
                    attempt_id=claim.attempt_id,
                    tool_sequence=tool_sequence,
                    tool_definition_id=descriptor.tool_definition_id,
                    upstream_name=descriptor.upstream_name,
                    fingerprint=descriptor.fingerprint,
                    arguments=arguments,
                ),
            )
            try:
                admission = gated.gate.admit(gated.request, now=self._clock())
                while admission is ApprovalAdmission.REQUESTED:
                    await asyncio.sleep(0.25)
                    if not self._authorize.check_permission(
                        run_id=run.id, tool_definition_id=descriptor.tool_definition_id
                    ).allowed:
                        gated.gate.abandon(gated.request, now=self._clock())
                        admission = ApprovalAdmission.REFUSED
                        break
                    admission = gated.gate.admit(gated.request, now=self._clock())
            except asyncio.CancelledError:
                gated.gate.abandon(gated.request, now=self._clock())
                self._invocations.mark_cancelled(
                    claim=claim, invocation_id=invocation_id, now=self._clock()
                )
                raise
            if admission is ApprovalAdmission.UNAVAILABLE:
                # Authority could not be confirmed. The invocation row already exists and must
                # not stay `requested` forever, so it closes as denied -- and the Run closes as
                # an internal failure, because a model must not be told which predicate failed
                # and nothing may be dispatched on an unchecked assumption.
                self._invocations.mark_denied(
                    claim=claim,
                    invocation_id=invocation_id,
                    decision=_approval_refused(),
                    now=self._clock(),
                )
                raise ModelProviderError(INTERNAL_EXECUTION_ERROR, usage=usage)
            if admission is not ApprovalAdmission.APPROVED:
                # The approval request is now durable (or already refused): the model sees the
                # ordinary Stage-D denial note and the owner decides out of band.
                self._invocations.mark_denied(
                    claim=claim,
                    invocation_id=invocation_id,
                    decision=_approval_refused(),
                    now=self._clock(),
                )
                return ToolInvocationOutcome(None, denied=True)

        if gated is not None:
            started = gated.gate.start(
                gated.request, claim=claim, invocation_id=invocation_id, now=self._clock()
            )
        else:
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
                if self._account_actions is not None and self._account_actions.applies(descriptor):
                    result = await self._account_actions.invoke(
                        run=run, claim=claim, descriptor=descriptor, arguments=arguments
                    )
                else:
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
