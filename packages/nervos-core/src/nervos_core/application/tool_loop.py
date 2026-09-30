"""The Think -> Act -> Observe loop: one Attempt, several model turns, bounded tool work.

This is the loop ADR 0017 describes. Four rules shape everything in it:

**The loop is NervOS's.** No provider SDK executes anything: an adapter reports that a model asked
for a tool, and the shared single-call mediator decides whether the call happens. The loop owns
catalog presentation and multi-turn orchestration; a provider shipping its own executor would
change nothing here.

**Authority is live, twice.** The catalog decides what may be *offered*; the shared mediator asks
D2's predicate freshly whether a specific call may *run* -- once when the call is requested, and
again inside the single transaction that commits the start. An entry in the catalog authorizes
nothing on its own.

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

import nervos_core.application.tool_invocation_mediator as _tool_invocation_mediator
from nervos_core.application.model_completion import (
    INTERNAL_EXECUTION_ERROR,
    MODEL_RATE_LIMITED,
    MODEL_RESPONSE_INVALID,
    TOOL_CATALOG_INVALID,
    TOOL_LOOP_LIMIT,
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
)
from nervos_core.application.tool_catalog import (
    ToolCatalog,
    ToolCatalogInvalid,
    ToolSourceSynchronizer,
    assemble_tool_catalog,
    gather_descriptors,
)
from nervos_core.application.tool_invocation_mediator import (
    ToolInvocationMediator,
    bounded_observation,
)
from nervos_core.application.tool_invocations import (
    ClaimHandle,
    ToolInvocationPersistence,
    parse_arguments,
)
from nervos_core.application.tool_permissions import ToolPermissionEvaluator
from nervos_core.application.tool_registry import ToolRegistry
from nervos_core.application.trusted_chat import ChatOutcome, final_chat_outcome
from nervos_core.domain.context import ContextSnapshotData
from nervos_core.domain.runs import Run, RunStatus
from nervos_core.domain.tools import ToolSourceRef

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
STRUCTURED_OMITTED_NOTE = _tool_invocation_mediator.STRUCTURED_OMITTED_NOTE
TEXT_TRUNCATED_NOTE = _tool_invocation_mediator.TEXT_TRUNCATED_NOTE

# Truncation disclosures. They are fixed, so a payload carrying one can be measured exactly before
# it is used, and they are short enough to fit inside the smallest legal result limit.
# Printable ASCII, the provider call-id contract. Every permitted character is one byte, so the
# byte bound and the character bound are the same bound.
_PRINTABLE_ASCII = frozenset(chr(code) for code in range(0x20, 0x7F))
MAX_PROVIDER_CALL_ID_BYTES = 128
MAX_DURABLE_ERROR_MESSAGE_CHARS = 512


def durable_error_message(error_code: str, message: str | None) -> str:
    """Bound a tool's safe static failure text to the durable audit column contract."""
    if message is not None and 1 <= len(message) <= MAX_DURABLE_ERROR_MESSAGE_CHARS:
        return message
    from nervos_core.application.model_completion import safe_error_message

    return safe_error_message(error_code)


class AttemptUsagePersistence(Protocol):
    """Durable aggregate usage for one multi-turn Attempt.

    Declared here, structurally, because the loop is its only caller and it must not depend on the
    orchestration module that happens to implement it alongside the rest of the Job writes.
    """

    def persist_attempt_usage(
        self, claim: ClaimHandle, *, usage: ModelUsage, now: datetime
    ) -> bool: ...


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
        mediator: ToolInvocationMediator | None = None,
    ) -> None:
        self._registry = registry
        self._source_ref = source_ref
        self._authorize = authorize
        self._usage = usage
        self._system_instruction = system_instruction
        self._clock = clock
        self._sleep = sleep
        self._synchronizer = synchronizer
        self._mediator = mediator or ToolInvocationMediator(
            registry=registry, authorize=authorize, invocations=invocations, clock=clock
        )

    async def run(
        self,
        completion: ModelCompletion,
        run: Run,
        claim: ClaimHandle,
        elapsed_ms: int,
        snapshot: ContextSnapshotData | None = None,
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
            response = await self._model_turn(completion, run, catalog, state, snapshot)
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
        snapshot: ContextSnapshotData | None = None,
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

        user_text = snapshot.current_user_text if snapshot is not None else run.input_text
        history = snapshot.history_messages if snapshot is not None else ()
        compaction = snapshot.injected_compaction_text if snapshot is not None else None
        user_mem = snapshot.injected_user_memory_text if snapshot is not None else None
        agent_mem = snapshot.injected_agent_memory_text if snapshot is not None else None

        request = ModelRequest(
            self._system_instruction,
            user_text,
            run.model_name,
            limits.max_output_tokens,
            limits.provider_timeout_ms,
            tools=catalog.schemas(),
            turns=tuple(state.turns),
            history=history,
            compaction_context=compaction,
            user_memory_context=user_mem,
            agent_memory_context=agent_mem,
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
        state.tool_sequence += 1
        outcome = await self._mediator.invoke(
            descriptor=entry.descriptor,
            arguments=arguments,
            run=run,
            claim=claim,
            tool_sequence=state.tool_sequence,
            provider_call_id=call.call_id,
            usage=state.usage,
        )
        if outcome.denied:
            self._observe_failure(run, state, call.call_id, _DENIED_NOTE)
            return
        if outcome.invalid_arguments:
            self._observe_failure(run, state, call.call_id, _INVALID_ARGUMENTS_NOTE)
            return
        result = outcome.result
        if result is None:
            raise ModelProviderError(INTERNAL_EXECUTION_ERROR, usage=state.usage)
        if outcome.failed:
            self._observe_failure(run, state, call.call_id, result.text)
            return
        observation = bounded_observation(
            text=result.text,
            structured=result.structured,
            is_error=False,
            max_bytes=run.limits.tool_result_max_bytes,
        )
        state.consecutive_failures = 0
        state.turns.append(
            ToolResultTurn(
                call_id=call.call_id,
                text=observation.text,
                structured=observation.structured,
                is_error=False,
            )
        )

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
        self._mediator.record_pre_dispatch_refusal(claim=claim, usage=state.usage)
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
