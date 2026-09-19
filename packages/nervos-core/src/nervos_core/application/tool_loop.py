"""The Think -> Act -> Observe loop: one Attempt, several model turns, bounded tool work.

This is the loop ADR 0017 describes. Four rules shape everything in it:

**The loop is NervOS's.** No provider SDK executes anything: an adapter reports that a model asked
for a tool, and this loop decides whether the call happens. A provider shipping its own executor
would change nothing here.

**Authority is live, twice.** The catalog decides what may be *offered*; D2's predicate decides,
freshly, whether a specific call may *run* -- once when the call is requested, and again inside the
single transaction that commits the start. An entry in the catalog authorizes nothing on its own.

**Every external effect is bracketed by durable state.** A tool call is recorded as `requested`
before it is dispatched and as `started` immediately before, so the boundary between "we intended to
call this" and "this may have happened" is a committed row rather than a comment. No database
transaction is ever open across a model call, a tool call, or a retry wait.

**Budgets belong to the Run.** `max_model_calls` counts real provider requests; `max_tool_calls`
counts every tool call the model produced. Both are snapshotted on the Run at submission and both
are checked *before* the work they bound, never after it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from nervos_core.application.model_completion import (
    EXECUTION_CANCELLED,
    INTERNAL_EXECUTION_ERROR,
    MODEL_RATE_LIMITED,
    MODEL_RESPONSE_INVALID,
    TOOL_CATALOG_INVALID,
    TOOL_LOOP_LIMIT,
    TOOL_OUTCOME_UNKNOWN,
    AssistantTurn,
    ConversationTurn,
    ModelCompletion,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    StopOutcome,
    ToolCall,
    ToolResultTurn,
    safe_error_message,
)
from nervos_core.application.tool_catalog import (
    ToolCatalog,
    ToolCatalogInvalid,
    ToolSourceSynchronizer,
    assemble_tool_catalog,
    gather_descriptors,
)
from nervos_core.application.tool_invocations import (
    ClaimHandle,
    InvocationRequest,
    RecordOutcomeKind,
    RefusalOutcomeKind,
    StartOutcomeKind,
    ToolInvocationPersistence,
    parse_arguments,
    result_envelope,
)
from nervos_core.application.tool_permissions import ToolPermissionEvaluator
from nervos_core.application.tool_registry import (
    ToolExecutionFailure,
    ToolOutcomeUnknown,
    ToolRegistry,
)
from nervos_core.application.tool_schema import validate_instance
from nervos_core.application.trusted_chat import ChatOutcome, final_chat_outcome
from nervos_core.domain.runs import Run, RunStatus
from nervos_core.domain.tools import JsonValue, ToolSourceRef, canonical_json_text

# Bounded in-Attempt retries for one rate-limited model turn. The ladder is the same frozen
# exponential Stage C already uses, reused rather than restated; there is no jitter, so a given
# sequence of provider outcomes always produces the same delays.
MAX_IN_ATTEMPT_MODEL_RETRIES = 3
_RETRY_DELAYS_SECONDS = (1.0, 2.0, 4.0)

# Bounded, static, value-free text. Nothing here can carry an argument, a result, a credential, or
# a provider payload: these are the only strings a failing call may put in front of the model.
_UNKNOWN_TOOL_NOTE = "The requested tool is not available to this agent."
_INVALID_ARGUMENTS_NOTE = "The tool call arguments were not valid for this tool."
_DENIED_NOTE = "The tool call was denied."

# Truncation disclosures. They are fixed, so a payload carrying one can be measured exactly before
# it is used, and they are short enough to fit inside the smallest legal result limit.
STRUCTURED_OMITTED_NOTE = "\n[NervOS: structured result omitted to fit this run's result limit.]"
TEXT_TRUNCATED_NOTE = "\n[NervOS: tool result truncated to fit this run's result limit.]"

# Printable ASCII, the provider call-id contract. Every permitted character is one byte, so the
# byte bound and the character bound are the same bound.
_PRINTABLE_ASCII = frozenset(chr(code) for code in range(0x20, 0x7F))
MAX_PROVIDER_CALL_ID_BYTES = 128

# The durable `tool_invocations` columns bound an error pair at 1..64 and 1..512 characters (D1's
# `error_bounds`). The message bound is restated here so the loop can guarantee what it writes is
# storable rather than discovering it as an integrity error after the call already ran.
MAX_DURABLE_ERROR_MESSAGE_CHARS = 512


class AttemptUsagePersistence(Protocol):
    """Durable aggregate usage for one multi-turn Attempt.

    Declared here, structurally, because the loop is its only caller and it must not depend on the
    orchestration module that happens to implement it alongside the rest of the Job writes.
    """

    def persist_attempt_usage(
        self, claim: ClaimHandle, *, usage: ModelUsage, now: datetime
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class Observation:
    """One tool result as the model will see it, and whether anything was withheld."""

    text: str
    structured: JsonValue | None
    is_error: bool
    truncated: bool


@dataclass
class _LoopState:
    """Mutable accounting for one Attempt. Never persisted, never shared across Attempts."""

    usage: ModelUsage = field(default_factory=ModelUsage)
    model_calls: int = 0
    tool_calls: int = 0
    tool_sequence: int = 0
    consecutive_failures: int = 0
    turns: list[ConversationTurn] = field(default_factory=lambda: [])
    seen_call_ids: set[str] = field(default_factory=lambda: set())


def is_valid_provider_call_id(value: str) -> bool:
    """Return whether a provider call identifier satisfies the frozen contract.

    A tool result is addressed back to the provider by exactly this value, so an identifier that
    cannot be echoed -- blank, non-printable, or over the bound -- would produce a continuation the
    provider cannot resolve. It is rejected rather than repaired, because a repaired identifier is
    not the one the provider issued.
    """
    if not 1 <= len(value) <= MAX_PROVIDER_CALL_ID_BYTES:
        return False
    return all(character in _PRINTABLE_ASCII for character in value)


def merge_usage(total: ModelUsage, reported: ModelUsage | None) -> ModelUsage:
    """Add one turn's reported usage into the Attempt aggregate.

    Each field is summed over the turns that actually reported it, and stays ``None`` only when no
    turn reported it -- trustworthy reported usage only, never estimated, derived, or completed.
    """
    if reported is None:
        return total
    return ModelUsage(
        input_tokens=_add(total.input_tokens, reported.input_tokens),
        output_tokens=_add(total.output_tokens, reported.output_tokens),
        total_tokens=_add(total.total_tokens, reported.total_tokens),
    )


def _add(left: int | None, right: int | None) -> int | None:
    if left is None:
        return right
    if right is None:
        return left
    return left + right


def bounded_observation(
    *,
    text: str,
    structured: JsonValue | None,
    is_error: bool,
    max_bytes: int,
) -> Observation:
    """Reduce one tool result to a model-visible payload inside `max_bytes`.

    Three deterministic steps, in order: the whole result; then the text with the structured value
    dropped whole and disclosed; then a UTF-8-safe head of the text with a truncation disclosed.
    Structured JSON is never partially emitted -- a sliced JSON document is not a smaller fact, it
    is a different and false one.
    """
    if _observation_bytes(text, structured) <= max_bytes:
        return Observation(text, structured, is_error, truncated=False)

    if structured is not None:
        omitted = text + STRUCTURED_OMITTED_NOTE
        if _observation_bytes(omitted, None) <= max_bytes:
            return Observation(omitted, None, is_error, truncated=True)

    budget = max_bytes - len(TEXT_TRUNCATED_NOTE.encode("utf-8"))
    return Observation(
        _utf8_head(text, budget) + TEXT_TRUNCATED_NOTE, None, is_error, truncated=True
    )


def _observation_bytes(text: str, structured: JsonValue | None) -> int:
    """Measure the provider-neutral model-visible payload.

    Provider wire wrappers -- block envelopes, JSON escaping -- are a provider's cost and are never
    what this limit bounds. What is bounded is what the observation itself says.
    """
    size = len(text.encode("utf-8"))
    if structured is not None:
        size += 1 + len(canonical_json_text(structured).encode("utf-8"))
    return size


def _utf8_head(text: str, budget: int) -> str:
    """Return the longest prefix of `text` fitting `budget` bytes, never splitting a code point."""
    if budget <= 0:
        return ""
    return text.encode("utf-8")[:budget].decode("utf-8", errors="ignore")


def durable_error_message(error_code: str, message: str | None) -> str:
    """Return an error message that the durable columns can actually store.

    A tool's declared message is bounded static text by its own contract, so this normally returns
    it unchanged. It exists for the case where it is not: an unrecordable message must not turn a
    *known* failure into an unrecorded one, because the invocation would then be left `started` --
    and `started` means "this may have had an effect", which is the opposite of what a tool that
    reported its own failure told us. Falling back to the code's static message keeps the row
    truthful, and the code still carries the classification.
    """
    if message is not None and 1 <= len(message) <= MAX_DURABLE_ERROR_MESSAGE_CHARS:
        return message
    return safe_error_message(error_code)


class ToolLoop:
    """Execute one tool-enabled Run as a bounded sequence of model turns and tool calls."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        source_ref: ToolSourceRef,
        authorize: ToolPermissionEvaluator,
        invocations: ToolInvocationPersistence,
        usage: AttemptUsagePersistence,
        system_instruction: str,
        clock: Callable[[], datetime],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        synchronizer: ToolSourceSynchronizer | None = None,
    ) -> None:
        self._registry = registry
        self._source_ref = source_ref
        self._authorize = authorize
        self._invocations = invocations
        self._usage = usage
        self._system_instruction = system_instruction
        self._clock = clock
        self._sleep = sleep
        self._synchronizer = synchronizer

    async def run(
        self,
        completion: ModelCompletion,
        run: Run,
        claim: ClaimHandle,
        elapsed_ms: int,
    ) -> ChatOutcome:
        """Run the loop and return the Run's final answer, or raise a normalized failure."""
        del elapsed_ms  # Measured by the coordinator; part of the handler contract only.
        limits = run.limits
        if run.status is not RunStatus.RUNNING or limits.max_tool_calls <= 0:
            # The loop is only ever reached for a running, tool-enabled Run. Anything else is a
            # routing defect, and the safe answer to one is to fail closed rather than guess.
            raise ModelProviderError(MODEL_RESPONSE_INVALID)

        catalog = await self._assemble_catalog(run)
        state = _LoopState()
        while True:
            response = await self._model_turn(completion, run, catalog, state)
            state.usage = merge_usage(state.usage, response.usage)
            await self._persist_usage(claim, state)

            calls = response.tool_calls
            if not calls:
                # A turn with no tool calls is the only shape that concludes the Run, and the
                # presence of tool calls -- never blank text -- is what decides it.
                return final_chat_outcome(response, run, state.usage)

            if state.tool_calls + len(calls) > limits.max_tool_calls:
                # Checked for the whole batch before any call in it runs, so an over-budget turn
                # can never leave one call executed and the next one refused.
                raise ModelProviderError(TOOL_LOOP_LIMIT, usage=state.usage)

            state.turns.append(AssistantTurn(text=response.text, tool_calls=calls))
            for call in calls:
                state.tool_calls += 1
                await self._dispatch(run, claim, catalog, state, call)

    async def _assemble_catalog(self, run: Run) -> ToolCatalog:
        """Assemble this Attempt's one immutable catalog, or fail the Run closed.

        Every registered source contributes, so one Attempt may offer built-ins alongside the tools
        of each MCP connection this process has synchronised. Membership still grants nothing: the
        assembly below submits every candidate to the live D2 evaluator.

        Synchronising first is what keeps the registry from being a startup-time snapshot. It runs
        *before* gathering and *outside* the provider call, it reads durable state only, and a
        failure here withholds tools rather than granting them -- so it cannot widen what this Run
        may do.
        """
        try:
            if self._synchronizer is not None:
                await self._synchronizer.synchronize_for_run(run)
            descriptors = await gather_descriptors(self._registry)
            return assemble_tool_catalog(
                descriptors=descriptors, run_id=run.id, authorize=self._authorize
            )
        except ToolCatalogInvalid as error:
            raise ModelProviderError(TOOL_CATALOG_INVALID) from error

    async def _model_turn(
        self,
        completion: ModelCompletion,
        run: Run,
        catalog: ToolCatalog,
        state: _LoopState,
    ) -> ModelResponse:
        """Issue one model turn, consuming model-call budget once per real provider request.

        A rate-limited turn is retried in place while the in-Attempt ladder and the Run's own
        model-call budget both allow it. The retry repeats only the current request against the same
        in-memory conversation: it never restarts the loop, never rebuilds from the immutable Run
        input, and never re-runs a tool that already succeeded.

        When the budget runs out mid-ladder the Run is still rate-limited, and it says so: reporting
        a loop limit here would replace a provider outcome with a NervOS one and silently change the
        Run's whole-Attempt retry eligibility.
        """
        limits = run.limits
        if state.model_calls >= limits.max_model_calls:
            raise ModelProviderError(TOOL_LOOP_LIMIT, usage=state.usage)

        request = ModelRequest(
            self._system_instruction,
            run.input_text,
            run.model_name,
            limits.max_output_tokens,
            limits.provider_timeout_ms,
            tools=catalog.schemas(),
            turns=tuple(state.turns),
        )
        retries = 0
        last_usage = state.usage
        while True:
            if state.model_calls >= limits.max_model_calls:
                # Only reachable while retrying: a fresh turn is admitted by the check above.
                raise ModelProviderError(MODEL_RATE_LIMITED, usage=last_usage)
            state.model_calls += 1
            try:
                response = await completion.complete(request)
            except ModelProviderError as error:
                if error.code != MODEL_RATE_LIMITED:
                    raise
                last_usage = merge_usage(state.usage, error.usage)
                if retries >= MAX_IN_ATTEMPT_MODEL_RETRIES:
                    raise ModelProviderError(MODEL_RATE_LIMITED, usage=last_usage) from error
                await self._sleep(_RETRY_DELAYS_SECONDS[retries])
                retries += 1
                continue
            self._validate_turn_shape(response)
            self._register_call_ids(response.tool_calls, state)
            return response

    @staticmethod
    def _validate_turn_shape(response: ModelResponse) -> None:
        """Enforce the provider-neutral turn contract before anything is done with the turn.

        A tool turn and a concluding turn are the only two shapes. Tool calls with a non-tool
        termination, or a tool termination with no calls, are both inconsistent: the first would
        leave calls the provider did not intend to be run, and the second would leave the loop with
        nothing to do and no answer.
        """
        if (response.finish_reason is StopOutcome.TOOL_USE) != bool(response.tool_calls):
            raise ModelProviderError(MODEL_RESPONSE_INVALID)

    @staticmethod
    def _register_call_ids(calls: Sequence[ToolCall], state: _LoopState) -> None:
        """Reject an unusable or reused call identifier before any call in the turn is dispatched.

        Uniqueness is per Attempt, not per turn: a continuation addresses results by call id, so a
        value reused across turns would make the conversation ambiguous. Nothing is renamed or
        repaired -- an invented identifier would be one the provider never issued.
        """
        batch: set[str] = set()
        for call in calls:
            if not is_valid_provider_call_id(call.call_id):
                raise ModelProviderError(MODEL_RESPONSE_INVALID)
            if call.call_id in batch or call.call_id in state.seen_call_ids:
                raise ModelProviderError(MODEL_RESPONSE_INVALID)
            batch.add(call.call_id)
        state.seen_call_ids |= batch

    async def _persist_usage(self, claim: ClaimHandle, state: _LoopState) -> None:
        """Make the aggregate Attempt usage durable *before* any tool call is dispatched.

        ADR 0017 requires the preceding model turns' usage to survive a crash that follows them, so
        a failure to record it stops the loop rather than being tolerated: continuing would perform
        an external effect whose cost has no durable record.
        """
        if self._usage.persist_attempt_usage(claim, usage=state.usage, now=self._clock()):
            return
        # Authority was lost, or the write could not commit. Nothing may be dispatched, and the Run
        # is closed as an internal failure: an aggregate that cannot be proven is never replayed.
        raise ModelProviderError(INTERNAL_EXECUTION_ERROR, usage=state.usage)

    async def _dispatch(
        self,
        run: Run,
        claim: ClaimHandle,
        catalog: ToolCatalog,
        state: _LoopState,
        call: ToolCall,
    ) -> None:
        """Perform one tool call, or record why it did not happen.

        Nothing here is retried and nothing is redispatched: a call that reached the ambiguity
        boundary has an outcome the loop cannot know, and inventing a second attempt would give one
        Requested call two effects.
        """
        entry = catalog.lookup(call.name)
        if entry is None:
            await self._refuse(run, claim, state, call.call_id, _UNKNOWN_TOOL_NOTE)
            return
        arguments = parse_arguments(call.arguments_json)
        if arguments is None:
            await self._refuse(run, claim, state, call.call_id, _INVALID_ARGUMENTS_NOTE)
            return
        if validate_instance(entry.canonical_input_schema, arguments) is not None:
            await self._refuse(run, claim, state, call.call_id, _INVALID_ARGUMENTS_NOTE)
            return

        descriptor = entry.descriptor
        decision = self._authorize.check_permission(
            run_id=run.id, tool_definition_id=descriptor.tool_definition_id
        )
        state.tool_sequence += 1
        requested = self._invocations.record_requested(
            claim=claim,
            request=InvocationRequest(
                run_id=run.id,
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                tool_sequence=state.tool_sequence,
                tool_definition_id=descriptor.tool_definition_id,
                source_kind=descriptor.source_kind,
                source_id=descriptor.source_id,
                upstream_name=descriptor.upstream_name,
                model_name=descriptor.model_name,
                definition_fingerprint=descriptor.fingerprint,
                provider_call_id=call.call_id,
                permission_decision=decision,
                arguments=arguments,
            ),
            now=self._clock(),
        )
        if requested.kind is RecordOutcomeKind.FENCED or requested.invocation_id is None:
            # No row exists, so nothing may be dispatched; the durable owner of this Job settles
            # its final state.
            raise ModelProviderError(INTERNAL_EXECUTION_ERROR, usage=state.usage)

        if not decision.allowed:
            # The first check already answered "was this allowed when requested?", and the answer
            # was no. The call is closed as denied rather than routed through the start boundary to
            # be refused a second time.
            self._invocations.mark_denied(
                claim=claim,
                invocation_id=requested.invocation_id,
                decision=decision,
                now=self._clock(),
            )
            self._observe_failure(run, state, call.call_id, _DENIED_NOTE)
            return

        started = self._invocations.mark_started(
            claim=claim, invocation_id=requested.invocation_id, now=self._clock()
        )
        if started.kind is StartOutcomeKind.FENCED:
            raise ModelProviderError(INTERNAL_EXECUTION_ERROR, usage=state.usage)
        if started.kind is StartOutcomeKind.CANCELLED:
            # Cancellation before the start boundary is a refusal, not ambiguity: the call
            # provably did not run, so the loop stops with no observation and the durable owner of
            # this Job settles the Run's terminal state.
            raise ModelProviderError(EXECUTION_CANCELLED, usage=state.usage)
        if started.kind is StartOutcomeKind.DENIED:
            self._observe_failure(run, state, call.call_id, _DENIED_NOTE)
            return

        executor = self._registry.executor(descriptor.source_ref)
        try:
            # The deadline is owned here, around the dispatch alone: a call that ran past it is
            # ambiguous rather than failed, because the timeout says nothing about whether the
            # effect landed.
            async with asyncio.timeout(run.limits.tool_timeout_ms / 1000):
                result = await executor.execute(descriptor, arguments)
        except TimeoutError:
            self._invocations.mark_ambiguous(
                claim=claim,
                invocation_id=requested.invocation_id,
                error_code=TOOL_OUTCOME_UNKNOWN,
                error_message=safe_error_message(TOOL_OUTCOME_UNKNOWN),
                now=self._clock(),
            )
            raise ModelProviderError(TOOL_OUTCOME_UNKNOWN, usage=state.usage) from None
        except ToolExecutionFailure as failure:
            self._failed_invocation(state, claim, requested.invocation_id, failure)
            self._observe_failure(run, state, call.call_id, failure.message)
            return
        except ToolOutcomeUnknown:
            # An executor that lost the answer rather than obtaining one. This is the same
            # ambiguity the timeout above models, reached deliberately instead of by accident: the
            # call may have landed, so it is recorded ambiguous and the Run fails without an
            # observation -- a model told "the result is unknown" would simply ask again, and one
            # Requested call may not produce two effects.
            self._invocations.mark_ambiguous(
                claim=claim,
                invocation_id=requested.invocation_id,
                error_code=TOOL_OUTCOME_UNKNOWN,
                error_message=safe_error_message(TOOL_OUTCOME_UNKNOWN),
                now=self._clock(),
            )
            raise ModelProviderError(TOOL_OUTCOME_UNKNOWN, usage=state.usage) from None
        except asyncio.CancelledError:
            # The caller withdrew the task -- an outer deadline or shutdown. The invocation is left
            # truthfully `started`: the call happened, and this Worker may not say how it ended.
            raise
        except Exception:
            # An unclassified exception is a defect in our own dispatch path, and it is never
            # reported as the tool's own rejection.
            self._close_failed(
                claim, requested.invocation_id, INTERNAL_EXECUTION_ERROR, usage=state.usage
            )
            raise ModelProviderError(INTERNAL_EXECUTION_ERROR, usage=state.usage) from None

        envelope = result_envelope(text=result.text, structured=result.structured)
        observation = bounded_observation(
            text=result.text,
            structured=result.structured,
            is_error=False,
            max_bytes=run.limits.tool_result_max_bytes,
        )
        if not self._invocations.mark_succeeded(
            claim=claim,
            invocation_id=requested.invocation_id,
            envelope=envelope,
            now=self._clock(),
        ):
            # The call happened, but its outcome could not be recorded. Continuing would build a
            # conversation on a call whose durable state is still `started`.
            raise ModelProviderError(INTERNAL_EXECUTION_ERROR, usage=state.usage)
        state.consecutive_failures = 0
        state.turns.append(
            ToolResultTurn(
                call_id=call.call_id,
                text=observation.text,
                structured=observation.structured,
                is_error=False,
            )
        )

    def _failed_invocation(
        self,
        state: _LoopState,
        claim: ClaimHandle,
        invocation_id: int,
        failure: ToolExecutionFailure,
    ) -> None:
        """Record a locally classified tool failure.

        The code is the tool's own closed vocabulary member and the message is the bounded static
        text D3 guarantees is safe to record, so nothing provider-supplied or argument-derived is
        ever persisted.
        """
        self._close_failed(
            claim, invocation_id, failure.reason.value, usage=state.usage, message=failure.message
        )

    def _close_failed(
        self,
        claim: ClaimHandle,
        invocation_id: int,
        error_code: str,
        *,
        usage: ModelUsage,
        message: str | None = None,
    ) -> None:
        if self._invocations.mark_failed(
            claim=claim,
            invocation_id=invocation_id,
            error_code=error_code,
            error_message=durable_error_message(error_code, message),
            now=self._clock(),
        ):
            return
        raise ModelProviderError(INTERNAL_EXECUTION_ERROR, usage=usage)

    async def _refuse(
        self,
        run: Run,
        claim: ClaimHandle,
        state: _LoopState,
        call_id: str,
        note: str,
    ) -> None:
        """Make a pre-dispatch refusal durable *before* the model is told anything about it.

        The durable fact comes first and the conversation follows it, never the reverse. If the
        audit write is refused because the Run was cancelled or because this Worker lost its
        authority, the loop must not append an observation and must not take another model turn:
        continuing would build a conversation on an Attempt that no longer owns its claim, and
        would present a refusal the timeline never recorded. So the authority result is propagated
        as the corresponding failure instead of being swallowed.

        A refusal that *is* recorded still counts exactly as it always did -- the same observation,
        the same consecutive-failure increment, the same loop limit -- because D6 adds an audit
        fact and changes no loop semantics.
        """
        outcome = self._invocations.record_pre_dispatch_refusal(claim=claim, now=self._clock())
        if outcome.kind is RefusalOutcomeKind.CANCELLED:
            raise ModelProviderError(EXECUTION_CANCELLED, usage=state.usage)
        if outcome.kind is RefusalOutcomeKind.FENCED:
            raise ModelProviderError(INTERNAL_EXECUTION_ERROR, usage=state.usage)
        self._observe_failure(run, state, call_id, note)

    def _observe_failure(self, run: Run, state: _LoopState, call_id: str, note: str) -> None:
        """Append one safe failure observation, failing the Run once the limit is reached.

        Every failure a model can see -- an unknown tool, unusable arguments, a denial, a tool's own
        classified failure -- counts the same way, because the limit bounds how long the loop will
        let the model keep asking, not how badly each attempt went. A fatal ambiguous outcome is
        never counted here: it ends the Run immediately and has no observation at all.
        """
        state.consecutive_failures += 1
        if state.consecutive_failures >= run.limits.max_consecutive_tool_failures:
            raise ModelProviderError(TOOL_LOOP_LIMIT, usage=state.usage)
        state.turns.append(ToolResultTurn(call_id=call_id, text=note, is_error=True))
