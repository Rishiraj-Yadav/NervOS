"""D4 loop behaviour: budgets, authority, turn shape, and the failure taxonomy.

Everything here runs against injected doubles, so each test states one rule of the loop without a
database or a provider. The durable transitions themselves are proved against a real schema in the
integration suite; what is proved here is the decision the loop makes before each of them.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import pytest
from nervos_core.application.model_completion import (
    EXECUTION_CANCELLED,
    INTERNAL_EXECUTION_ERROR,
    MODEL_RATE_LIMITED,
    MODEL_RESPONSE_INVALID,
    TOOL_CATALOG_INVALID,
    TOOL_LOOP_LIMIT,
    TOOL_OUTCOME_UNKNOWN,
    AssistantTurn,
    ModelProviderError,
    ModelResponse,
    ModelUsage,
    StopOutcome,
    ToolCall,
    ToolResultTurn,
    safe_error_message,
)
from nervos_core.application.tool_invocations import (
    ALLOWED_DECISION,
    ClaimHandle,
    InvocationRequest,
    InvocationStatus,
    RecordOutcome,
    RecordOutcomeKind,
    ResultEnvelope,
    StartOutcome,
    StartOutcomeKind,
    permission_decision_value,
)
from nervos_core.application.tool_loop import (
    MAX_IN_ATTEMPT_MODEL_RETRIES,
    STRUCTURED_OMITTED_NOTE,
    TEXT_TRUNCATED_NOTE,
    durable_error_message,
    merge_usage,
)
from nervos_core.application.tool_permissions import (
    PermissionDecision,
    PermissionDenialReason,
)
from nervos_core.application.tool_registry import (
    ToolExecutionFailure,
    ToolFailureReason,
    ToolRegistry,
    ToolResult,
)
from nervos_core.application.trusted_chat import NERVOS_TOOL_CHAT_SYSTEM_INSTRUCTION
from nervos_core.domain.runs import TOOL_ENABLED_LIMITS, Run, RunLimits, RunStatus
from nervos_core.domain.tools import (
    RiskHints,
    ToolDescriptor,
    ToolSourceKind,
    ToolSourceRef,
)

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
BUILTIN_REF = ToolSourceRef(ToolSourceKind.BUILTIN, None)


@dataclass(frozen=True, slots=True)
class _Claim:
    job_id: int = 1
    run_id: int = 1
    attempt_id: int = 1
    attempt_number: int = 1
    worker_id: str = "worker-1"
    claim_token: bytes = b"t" * 32
    lease_expires_at: datetime = NOW


CLAIM: ClaimHandle = _Claim()


def _run(limits: RunLimits = TOOL_ENABLED_LIMITS) -> Run:
    return Run(
        1,
        1,
        "nervos.chat",
        "2",
        "anthropic",
        "opaque/model",
        "what time is it",
        limits,
        RunStatus.RUNNING,
        NOW,
        started_at=NOW,
    )


def _descriptor(name: str, *, tool_definition_id: int = 1) -> ToolDescriptor:
    return ToolDescriptor(
        tool_definition_id=tool_definition_id,
        upstream_name=name,
        model_name=f"nervos__builtin__{name}",
        source_kind=ToolSourceKind.BUILTIN,
        source_id=None,
        display_name=name,
        description=f"The {name} tool.",
        input_schema={
            "type": "object",
            "properties": {"argument": {"type": "string"}},
        },
        output_schema=None,
        risk_hints=RiskHints(),
        fingerprint="0" * 64,
    )


def _call(name: str, arguments: dict[str, Any], *, call_id: str = "call-1") -> ToolCall:
    return ToolCall(
        call_id=call_id,
        name=f"nervos__builtin__{name}",
        arguments_json=json.dumps(arguments, sort_keys=True, separators=(",", ":")),
    )


class _Completion:
    """A scripted provider: each entry is a response or an exception to raise."""

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.requests: list[Any] = []

    async def complete(self, request: Any) -> ModelResponse:
        self.requests.append(request)
        if not self.script:
            raise AssertionError("the loop made an unscripted provider call")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _Source:
    def __init__(self, descriptors: tuple[ToolDescriptor, ...]) -> None:
        self._descriptors = descriptors

    async def list_tools(self) -> tuple[ToolDescriptor, ...]:
        return self._descriptors


class _Executor:
    """A tool double recording each dispatch and returning one scripted outcome."""

    def __init__(self, outcome: Any = None) -> None:
        self.calls: list[tuple[ToolDescriptor, dict[str, Any]]] = []
        self._outcome = outcome

    async def execute(self, descriptor: ToolDescriptor, arguments: Any) -> ToolResult:
        self.calls.append((descriptor, dict(arguments)))
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        if callable(self._outcome):
            produced = self._outcome(descriptor, dict(arguments))
            if inspect.isawaitable(produced):
                return await produced
            assert isinstance(produced, ToolResult)
            return produced
        if self._outcome is not None:
            return self._outcome
        return ToolResult(text=f"ran {descriptor.upstream_name}")


class _Authorizer:
    def __init__(self, allowed: bool = True, decisions: list[bool] | None = None) -> None:
        self.allowed = allowed
        self.decisions = list(decisions) if decisions is not None else None
        self.calls: list[int] = []

    def check_permission(self, *, run_id: int, tool_definition_id: int) -> PermissionDecision:
        self.calls.append(tool_definition_id)
        allowed = self.decisions.pop(0) if self.decisions else self.allowed
        if allowed:
            return PermissionDecision(allowed=True)
        return PermissionDecision(allowed=False, reason=PermissionDenialReason.NOT_GRANTED)


@dataclass
class _Stored:
    invocation_id: int
    request: InvocationRequest
    status: str = InvocationStatus.REQUESTED.value
    decision: str = ALLOWED_DECISION
    envelope: ResultEnvelope | None = None
    error_code: str | None = None


class _Invocations:
    """In-memory invocation store mirroring the durable lifecycle's transitions."""

    def __init__(
        self, *, record: str = "requested", start: str = "started", conclude: bool = True
    ) -> None:
        self.rows: list[_Stored] = []
        self._next_id = 1
        self._record = record
        self._start = start
        self._conclude = conclude

    def record_requested(
        self, *, claim: ClaimHandle, request: InvocationRequest, now: datetime
    ) -> RecordOutcome:
        if self._record == "fenced":
            return RecordOutcome(RecordOutcomeKind.FENCED)
        row = _Stored(
            self._next_id,
            request,
            decision=permission_decision_value(request.permission_decision),
        )
        self.rows.append(row)
        self._next_id += 1
        return RecordOutcome(RecordOutcomeKind.REQUESTED, row.invocation_id)

    def mark_started(
        self, *, claim: ClaimHandle, invocation_id: int, now: datetime
    ) -> StartOutcome:
        if self._start == "fenced":
            return StartOutcome(StartOutcomeKind.FENCED)
        if self._start == "cancelled":
            return StartOutcome(StartOutcomeKind.CANCELLED)
        row = self._row(invocation_id)
        row.status = InvocationStatus.STARTED.value
        return StartOutcome(StartOutcomeKind.STARTED, invocation_id, ALLOWED_DECISION)

    def mark_denied(
        self,
        *,
        claim: ClaimHandle,
        invocation_id: int,
        decision: PermissionDecision,
        now: datetime,
    ) -> bool:
        row = self._row(invocation_id)
        row.status = InvocationStatus.DENIED.value
        row.decision = permission_decision_value(decision)
        return True

    def mark_cancelled(self, *, claim: ClaimHandle, invocation_id: int, now: datetime) -> bool:
        self._row(invocation_id).status = InvocationStatus.CANCELLED.value
        return True

    def mark_succeeded(
        self,
        *,
        claim: ClaimHandle,
        invocation_id: int,
        envelope: ResultEnvelope,
        now: datetime,
    ) -> bool:
        if not self._conclude:
            return False
        row = self._row(invocation_id)
        row.status = InvocationStatus.SUCCEEDED.value
        row.envelope = envelope
        return True

    def mark_failed(
        self,
        *,
        claim: ClaimHandle,
        invocation_id: int,
        error_code: str,
        error_message: str,
        now: datetime,
    ) -> bool:
        row = self._row(invocation_id)
        row.status = InvocationStatus.FAILED.value
        row.error_code = error_code
        return True

    def mark_ambiguous(
        self,
        *,
        claim: ClaimHandle,
        invocation_id: int,
        error_code: str,
        error_message: str,
        now: datetime,
    ) -> bool:
        self._row(invocation_id).status = InvocationStatus.AMBIGUOUS.value
        return True

    def _row(self, invocation_id: int) -> _Stored:
        return next(row for row in self.rows if row.invocation_id == invocation_id)

    def statuses(self) -> list[str]:
        return [row.status for row in self.rows]


@dataclass
class _Usage:
    persisted: list[ModelUsage] = field(default_factory=lambda: [])
    fail: bool = False

    def persist_attempt_usage(
        self, claim: ClaimHandle, *, usage: ModelUsage, now: datetime
    ) -> bool:
        if self.fail:
            return False
        self.persisted.append(usage)
        return True


def _loop(
    *,
    completion: _Completion,
    descriptors: tuple[ToolDescriptor, ...] = (_descriptor("current_time"),),
    executor: _Executor | None = None,
    authorizer: _Authorizer | None = None,
    invocations: _Invocations | None = None,
    usage: _Usage | None = None,
    sleeps: list[float] | None = None,
) -> tuple[Any, _Executor, _Invocations, _Usage, list[float]]:
    from nervos_core.application.tool_loop import ToolLoop

    executor = executor or _Executor()
    invocations = invocations or _Invocations()
    usage = usage or _Usage()
    delays = sleeps if sleeps is not None else []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    registry = ToolRegistry()
    registry.register(source_ref=BUILTIN_REF, source=_Source(descriptors), executor=executor)
    loop = ToolLoop(
        registry=registry,
        source_ref=BUILTIN_REF,
        authorize=authorizer or _Authorizer(),
        invocations=invocations,
        usage=usage,
        system_instruction=NERVOS_TOOL_CHAT_SYSTEM_INSTRUCTION,
        clock=lambda: NOW,
        sleep=sleep,
    )
    return loop, executor, invocations, usage, delays


def _final(text: str = "the answer") -> ModelResponse:
    return ModelResponse(text, "anthropic", "opaque/model", StopOutcome.STOP, ModelUsage(1, 2, 3))


def _tool_turn(*calls: ToolCall) -> ModelResponse:
    return ModelResponse(
        "", "anthropic", "opaque/model", StopOutcome.TOOL_USE, ModelUsage(1, 1, 2), tuple(calls)
    )


async def _run_loop(loop: Any, run: Run, completion: _Completion) -> Any:
    return await loop.run(completion, run, CLAIM, 0)


class TestHappyPath:
    @pytest.mark.anyio
    async def test_a_direct_answer_needs_no_tool(self) -> None:
        completion = _Completion(_final("done"))
        loop, executor, _, usage, _ = _loop(completion=completion)

        outcome = await _run_loop(loop, _run(), completion)

        assert outcome.output_text == "done"
        assert executor.calls == []
        assert usage.persisted == [ModelUsage(1, 2, 3)]

    @pytest.mark.anyio
    async def test_one_tool_call_is_executed_and_its_result_reaches_the_next_turn(self) -> None:
        completion = _Completion(_tool_turn(_call("current_time", {"argument": "x"})), _final())
        loop, executor, invocations, _, _ = _loop(
            completion=completion,
            executor=_Executor(ToolResult(text="12:00", structured={"h": 12})),
        )

        outcome = await _run_loop(loop, _run(), completion)

        assert outcome.output_text == "the answer"
        assert len(executor.calls) == 1
        assert invocations.statuses() == [InvocationStatus.SUCCEEDED.value]

        # The second request carries both the assistant turn and the result, addressed by call id.
        turns = completion.requests[1].turns
        assert turns[0] == AssistantTurn(
            text="", tool_calls=(_call("current_time", {"argument": "x"}),)
        )
        assert isinstance(turns[1], ToolResultTurn)
        assert turns[1].call_id == "call-1"
        assert turns[1].text == "12:00"
        assert turns[1].structured == {"h": 12}

    @pytest.mark.anyio
    async def test_the_catalog_is_assembled_once_and_offered_on_every_turn(self) -> None:
        completion = _Completion(_tool_turn(_call("current_time", {"argument": "x"})), _final())
        loop, _, _, _, _ = _loop(completion=completion)

        await _run_loop(loop, _run(), completion)

        for request in completion.requests:
            assert [schema.name for schema in request.tools] == ["nervos__builtin__current_time"]

    @pytest.mark.anyio
    async def test_multiple_calls_in_one_turn_run_sequentially_in_provider_order(self) -> None:
        completion = _Completion(
            _tool_turn(
                _call("current_time", {"argument": "1"}, call_id="a"),
                _call("current_time", {"argument": "2"}, call_id="b"),
            ),
            _final(),
        )
        loop, executor, invocations, _, _ = _loop(completion=completion)

        await _run_loop(loop, _run(), completion)

        assert [arguments for _, arguments in executor.calls] == [
            {"argument": "1"},
            {"argument": "2"},
        ]
        assert [row.request.provider_call_id for row in invocations.rows] == ["a", "b"]
        assert [row.request.tool_sequence for row in invocations.rows] == [1, 2]


class TestModelCallBudget:
    @pytest.mark.anyio
    async def test_every_provider_request_consumes_one_unit(self) -> None:
        limits = replace(TOOL_ENABLED_LIMITS, max_model_calls=3)
        completion = _Completion(
            _tool_turn(_call("current_time", {"argument": "x"})),
            _tool_turn(_call("current_time", {"argument": "y"}, call_id="c2")),
            _final(),
        )
        loop, _, _, _, _ = _loop(completion=completion)

        outcome = await _run_loop(loop, _run(limits), completion)

        assert outcome.output_text == "the answer"
        assert len(completion.requests) == 3

    @pytest.mark.anyio
    async def test_a_fresh_turn_beyond_the_budget_is_a_loop_limit(self) -> None:
        limits = replace(TOOL_ENABLED_LIMITS, max_model_calls=1)
        completion = _Completion(_tool_turn(_call("current_time", {"argument": "x"})))
        loop, executor, _, _, _ = _loop(completion=completion)

        with pytest.raises(ModelProviderError) as raised:
            await _run_loop(loop, _run(limits), completion)

        assert raised.value.code == TOOL_LOOP_LIMIT
        # The budget was available when the tool turn was issued, so that turn's call legitimately
        # ran; the limit is reached only when the loop wants a *new* turn it cannot afford.
        assert len(executor.calls) == 1
        assert len(completion.requests) == 1

    @pytest.mark.anyio
    async def test_rate_limit_retries_also_consume_the_same_budget(self) -> None:
        limits = replace(TOOL_ENABLED_LIMITS, max_model_calls=2)
        completion = _Completion(
            ModelProviderError(MODEL_RATE_LIMITED),
            ModelProviderError(MODEL_RATE_LIMITED),
        )
        loop, _, _, _, delays = _loop(completion=completion)

        with pytest.raises(ModelProviderError) as raised:
            await _run_loop(loop, _run(limits), completion)

        # Both units were spent on the same logical turn, so no third request may be made -- and
        # the outcome stays the provider's rate limit rather than becoming a NervOS loop limit.
        assert raised.value.code == MODEL_RATE_LIMITED
        assert len(completion.requests) == 2
        assert delays == [1.0, 2.0]


class TestInAttemptRetries:
    @pytest.mark.anyio
    async def test_the_ladder_is_one_two_four_seconds_with_no_jitter(self) -> None:
        completion = _Completion(
            ModelProviderError(MODEL_RATE_LIMITED),
            ModelProviderError(MODEL_RATE_LIMITED),
            ModelProviderError(MODEL_RATE_LIMITED),
            _final(),
        )
        loop, _, _, _, delays = _loop(completion=completion)

        await _run_loop(loop, _run(), completion)

        assert delays == [1.0, 2.0, 4.0]
        assert len(completion.requests) == 4

    @pytest.mark.anyio
    async def test_exhausting_the_ladder_propagates_the_provider_rate_limit(self) -> None:
        completion = _Completion(*[ModelProviderError(MODEL_RATE_LIMITED)] * 8)
        loop, _, _, _, delays = _loop(completion=completion)

        with pytest.raises(ModelProviderError) as raised:
            await _run_loop(loop, _run(), completion)

        # A provider outcome must never be relabelled as a NervOS loop limit.
        assert raised.value.code == MODEL_RATE_LIMITED
        assert delays == [1.0, 2.0, 4.0]
        assert len(completion.requests) == MAX_IN_ATTEMPT_MODEL_RETRIES + 1

    @pytest.mark.anyio
    async def test_a_retry_after_a_successful_tool_does_not_repeat_the_tool(self) -> None:
        completion = _Completion(
            _tool_turn(_call("current_time", {"argument": "x"})),
            ModelProviderError(MODEL_RATE_LIMITED),
            _final(),
        )
        loop, executor, invocations, _, delays = _loop(completion=completion)

        outcome = await _run_loop(loop, _run(), completion)

        assert outcome.output_text == "the answer"
        assert len(executor.calls) == 1
        assert invocations.statuses() == [InvocationStatus.SUCCEEDED.value]
        assert delays == [1.0]
        # The retry re-issued the same conversation, so the assistant turn is unchanged.
        assert completion.requests[1].turns == completion.requests[2].turns

    @pytest.mark.anyio
    async def test_a_non_rate_limit_provider_error_is_not_retried(self) -> None:
        completion = _Completion(ModelProviderError("model_unavailable"))
        loop, _, _, _, delays = _loop(completion=completion)

        with pytest.raises(ModelProviderError) as raised:
            await _run_loop(loop, _run(), completion)

        assert raised.value.code == "model_unavailable"
        assert delays == []


class TestToolCallBudget:
    @pytest.mark.anyio
    async def test_an_oversized_batch_dispatches_none_of_its_calls(self) -> None:
        limits = replace(TOOL_ENABLED_LIMITS, max_tool_calls=1)
        completion = _Completion(
            _tool_turn(
                _call("current_time", {"argument": "1"}, call_id="a"),
                _call("current_time", {"argument": "2"}, call_id="b"),
            )
        )
        loop, executor, invocations, _, _ = _loop(completion=completion)

        with pytest.raises(ModelProviderError) as raised:
            await _run_loop(loop, _run(limits), completion)

        assert raised.value.code == TOOL_LOOP_LIMIT
        assert executor.calls == []
        assert invocations.rows == []

    @pytest.mark.anyio
    async def test_the_budget_is_checked_across_turns_not_just_within_one(self) -> None:
        limits = replace(TOOL_ENABLED_LIMITS, max_tool_calls=1)
        completion = _Completion(
            _tool_turn(_call("current_time", {"argument": "1"}, call_id="a")),
            _tool_turn(_call("current_time", {"argument": "2"}, call_id="b")),
        )
        loop, executor, _, _, _ = _loop(completion=completion)

        with pytest.raises(ModelProviderError) as raised:
            await _run_loop(loop, _run(limits), completion)

        assert raised.value.code == TOOL_LOOP_LIMIT
        assert len(executor.calls) == 1

    @pytest.mark.anyio
    async def test_unknown_malformed_and_denied_calls_all_consume_budget(self) -> None:
        """The budget counts what the model asked for, not what the agent could carry out."""
        limits = replace(TOOL_ENABLED_LIMITS, max_tool_calls=3)
        completion = _Completion(
            _tool_turn(
                _call("absent", {"argument": "1"}, call_id="a"),
                ToolCall(call_id="b", name="nervos__builtin__current_time", arguments_json="[]"),
                _call("current_time", {"argument": "3"}, call_id="c"),
            ),
            # A fourth request, so the run can only continue if the first three were not counted.
            _tool_turn(_call("current_time", {"argument": "4"}, call_id="d")),
        )
        loop, executor, invocations, _, _ = _loop(completion=completion)

        with pytest.raises(ModelProviderError) as raised:
            await _run_loop(loop, _run(limits), completion)

        assert raised.value.code == TOOL_LOOP_LIMIT
        # Only the well-formed, available call reached the executor; the other two still counted.
        assert len(executor.calls) == 1
        assert invocations.statuses() == [InvocationStatus.SUCCEEDED.value]

    @pytest.mark.anyio
    async def test_a_call_denied_after_the_catalog_was_assembled_is_still_refused(self) -> None:
        """The catalog bounds what is offered; the live predicate bounds what is done.

        A grant revoked between assembly and dispatch must still stop the call, which is exactly
        what the second, live check inside the start transaction exists to catch.
        """
        limits = replace(TOOL_ENABLED_LIMITS, max_tool_calls=2)
        completion = _Completion(
            _tool_turn(_call("current_time", {"argument": "1"}, call_id="a")),
            _final(),
        )
        # First call is catalog assembly (allowed); second is the dispatch-time check (denied).
        loop, executor, invocations, _, _ = _loop(
            completion=completion, authorizer=_Authorizer(decisions=[True, False])
        )

        outcome = await _run_loop(loop, _run(limits), completion)

        assert outcome.output_text == "the answer"
        assert executor.calls == []
        assert invocations.statuses() == [InvocationStatus.DENIED.value]


class TestTurnShape:
    @pytest.mark.anyio
    async def test_tool_calls_without_a_tool_termination_are_refused(self) -> None:
        completion = _Completion(
            ModelResponse(
                "text",
                "anthropic",
                "opaque/model",
                StopOutcome.STOP,
                ModelUsage(),
                (_call("current_time", {"argument": "x"}),),
            )
        )
        loop, executor, _, _, _ = _loop(completion=completion)

        with pytest.raises(ModelProviderError) as raised:
            await _run_loop(loop, _run(), completion)

        assert raised.value.code == MODEL_RESPONSE_INVALID
        assert executor.calls == []

    @pytest.mark.anyio
    async def test_a_tool_termination_with_no_calls_is_refused(self) -> None:
        completion = _Completion(
            ModelResponse("", "anthropic", "opaque/model", StopOutcome.TOOL_USE, ModelUsage())
        )
        loop, _, _, _, _ = _loop(completion=completion)

        with pytest.raises(ModelProviderError) as raised:
            await _run_loop(loop, _run(), completion)

        assert raised.value.code == MODEL_RESPONSE_INVALID

    @pytest.mark.anyio
    async def test_blank_text_with_tool_calls_is_still_a_valid_tool_turn(self) -> None:
        completion = _Completion(_tool_turn(_call("current_time", {"argument": "x"})), _final())
        loop, executor, _, _, _ = _loop(completion=completion)

        await _run_loop(loop, _run(), completion)

        assert len(executor.calls) == 1

    @pytest.mark.anyio
    async def test_prose_beside_tool_calls_is_preserved_but_never_the_final_answer(self) -> None:
        completion = _Completion(
            ModelResponse(
                "let me check",
                "anthropic",
                "opaque/model",
                StopOutcome.TOOL_USE,
                ModelUsage(),
                (_call("current_time", {"argument": "x"}),),
            ),
            _final("the real answer"),
        )
        loop, _, _, _, _ = _loop(completion=completion)

        outcome = await _run_loop(loop, _run(), completion)

        assert outcome.output_text == "the real answer"
        assert completion.requests[1].turns[0].text == "let me check"

    @pytest.mark.anyio
    async def test_a_blank_final_answer_is_refused(self) -> None:
        completion = _Completion(ModelResponse("", "anthropic", "opaque/model", StopOutcome.STOP))
        loop, _, _, _, _ = _loop(completion=completion)

        with pytest.raises(ModelProviderError) as raised:
            await _run_loop(loop, _run(), completion)

        assert raised.value.code == MODEL_RESPONSE_INVALID


class TestCallIdentifiers:
    @pytest.mark.anyio
    async def test_a_non_printable_call_id_is_refused_before_any_dispatch(self) -> None:
        completion = _Completion(
            _tool_turn(_call("current_time", {"argument": "x"}, call_id="bad\nid"))
        )
        loop, executor, invocations, _, _ = _loop(completion=completion)

        with pytest.raises(ModelProviderError) as raised:
            await _run_loop(loop, _run(), completion)

        assert raised.value.code == MODEL_RESPONSE_INVALID
        assert executor.calls == []
        assert invocations.rows == []

    @pytest.mark.anyio
    async def test_a_duplicate_call_id_within_one_turn_is_refused(self) -> None:
        completion = _Completion(
            _tool_turn(
                _call("current_time", {"argument": "1"}, call_id="same"),
                _call("current_time", {"argument": "2"}, call_id="same"),
            )
        )
        loop, executor, _, _, _ = _loop(completion=completion)

        with pytest.raises(ModelProviderError) as raised:
            await _run_loop(loop, _run(), completion)

        assert raised.value.code == MODEL_RESPONSE_INVALID
        assert executor.calls == []

    @pytest.mark.anyio
    async def test_a_call_id_reused_across_turns_is_refused(self) -> None:
        completion = _Completion(
            _tool_turn(_call("current_time", {"argument": "1"}, call_id="same")),
            _tool_turn(_call("current_time", {"argument": "2"}, call_id="same")),
        )
        loop, executor, _, _, _ = _loop(completion=completion)

        with pytest.raises(ModelProviderError) as raised:
            await _run_loop(loop, _run(), completion)

        assert raised.value.code == MODEL_RESPONSE_INVALID
        # The first turn's call ran; the second turn's reuse must never reach the executor.
        assert len(executor.calls) == 1


class TestUsageDurability:
    @pytest.mark.anyio
    async def test_usage_is_durable_before_the_executor_is_entered(self) -> None:
        seen: list[int] = []

        async def observe(descriptor: ToolDescriptor, arguments: dict[str, Any]) -> ToolResult:
            seen.append(len(usage.persisted))
            return ToolResult(text="ok")

        completion = _Completion(_tool_turn(_call("current_time", {"argument": "x"})), _final())
        loop, _, _, usage, _ = _loop(completion=completion, executor=_Executor(observe))

        await _run_loop(loop, _run(), completion)

        # The model turn's usage was recorded before the tool ran, not after.
        assert seen == [1]

    @pytest.mark.anyio
    async def test_usage_accumulates_across_turns(self) -> None:
        completion = _Completion(_tool_turn(_call("current_time", {"argument": "x"})), _final())
        loop, _, _, usage, _ = _loop(completion=completion)

        await _run_loop(loop, _run(), completion)

        assert usage.persisted == [ModelUsage(1, 1, 2), ModelUsage(2, 3, 5)]

    @pytest.mark.anyio
    async def test_a_failed_usage_write_prevents_any_dispatch(self) -> None:
        completion = _Completion(_tool_turn(_call("current_time", {"argument": "x"})), _final())
        loop, executor, invocations, _, _ = _loop(completion=completion, usage=_Usage(fail=True))

        with pytest.raises(ModelProviderError) as raised:
            await _run_loop(loop, _run(), completion)

        assert raised.value.code == INTERNAL_EXECUTION_ERROR
        assert executor.calls == []
        assert invocations.rows == []


class TestAuthority:
    @pytest.mark.anyio
    async def test_an_initial_denial_is_recorded_as_denied_with_no_executor_call(self) -> None:
        completion = _Completion(_tool_turn(_call("current_time", {"argument": "x"})), _final())
        loop, executor, invocations, _, _ = _loop(
            completion=completion, authorizer=_Authorizer(decisions=[True, False])
        )

        outcome = await _run_loop(loop, _run(), completion)

        assert outcome.output_text == "the answer"
        assert executor.calls == []
        assert invocations.statuses() == [InvocationStatus.DENIED.value]
        assert invocations.rows[0].decision == "denied_not_granted"

    @pytest.mark.anyio
    async def test_cancellation_before_the_start_boundary_stops_without_dispatching(self) -> None:
        completion = _Completion(_tool_turn(_call("current_time", {"argument": "x"})), _final())
        loop, executor, invocations, _, _ = _loop(
            completion=completion, invocations=_Invocations(start="cancelled")
        )

        with pytest.raises(ModelProviderError) as raised:
            await _run_loop(loop, _run(), completion)

        # The call never started, so this is a refusal reported as cancellation -- never the
        # ambiguity code, which would wrongly imply the outcome is unknown.
        assert raised.value.code == EXECUTION_CANCELLED
        assert executor.calls == []
        assert invocations.statuses() == [InvocationStatus.REQUESTED.value]

    @pytest.mark.anyio
    async def test_a_tool_that_outruns_its_deadline_is_marked_ambiguous(self) -> None:
        completion = _Completion(_tool_turn(_call("current_time", {"argument": "x"})))

        async def hang(descriptor: ToolDescriptor, arguments: dict[str, Any]) -> ToolResult:
            await asyncio.sleep(30)
            return ToolResult(text="never")

        loop, _, invocations, _, _ = _loop(completion=completion, executor=_Executor(hang))
        limits = replace(TOOL_ENABLED_LIMITS, tool_timeout_ms=1000)

        with pytest.raises(ModelProviderError) as raised:
            await _run_loop(loop, _run(limits), completion)

        assert raised.value.code == TOOL_OUTCOME_UNKNOWN
        assert invocations.statuses() == [InvocationStatus.AMBIGUOUS.value]

    @pytest.mark.anyio
    async def test_a_lost_fence_stops_the_loop_without_dispatching(self) -> None:
        completion = _Completion(_tool_turn(_call("current_time", {"argument": "x"})))
        loop, executor, invocations, _, _ = _loop(
            completion=completion, invocations=_Invocations(start="fenced")
        )

        with pytest.raises(ModelProviderError) as raised:
            await _run_loop(loop, _run(), completion)

        assert raised.value.code == INTERNAL_EXECUTION_ERROR
        assert executor.calls == []
        assert invocations.statuses() == [InvocationStatus.REQUESTED.value]

    @pytest.mark.anyio
    async def test_a_refused_record_inserts_nothing_and_dispatches_nothing(self) -> None:
        completion = _Completion(_tool_turn(_call("current_time", {"argument": "x"})))
        loop, executor, invocations, _, _ = _loop(
            completion=completion, invocations=_Invocations(record="fenced")
        )

        with pytest.raises(ModelProviderError) as raised:
            await _run_loop(loop, _run(), completion)

        assert raised.value.code == INTERNAL_EXECUTION_ERROR
        assert executor.calls == []
        assert invocations.rows == []

    @pytest.mark.anyio
    async def test_a_terminal_write_that_cannot_commit_stops_the_run(self) -> None:
        completion = _Completion(_tool_turn(_call("current_time", {"argument": "x"})))
        loop, executor, invocations, _, _ = _loop(
            completion=completion, invocations=_Invocations(conclude=False)
        )

        with pytest.raises(ModelProviderError) as raised:
            await _run_loop(loop, _run(), completion)

        assert raised.value.code == INTERNAL_EXECUTION_ERROR
        assert len(executor.calls) == 1
        assert invocations.statuses() == [InvocationStatus.STARTED.value]


class TestFailureTaxonomy:
    @pytest.mark.anyio
    async def test_a_classified_tool_failure_continues_with_a_safe_observation(self) -> None:
        failure = ToolExecutionFailure(ToolFailureReason.ARGUMENTS_INVALID, "Bad arguments.")
        completion = _Completion(_tool_turn(_call("current_time", {"argument": "x"})), _final())
        loop, _, invocations, _, _ = _loop(completion=completion, executor=_Executor(failure))

        outcome = await _run_loop(loop, _run(), completion)

        assert outcome.output_text == "the answer"
        assert invocations.statuses() == [InvocationStatus.FAILED.value]
        assert invocations.rows[0].error_code == ToolFailureReason.ARGUMENTS_INVALID.value
        assert completion.requests[1].turns[1].text == "Bad arguments."

    @pytest.mark.anyio
    async def test_three_consecutive_failures_end_the_run(self) -> None:
        failure = ToolExecutionFailure(ToolFailureReason.ARGUMENTS_INVALID, "Bad arguments.")
        calls = tuple(
            _call("current_time", {"argument": str(index)}, call_id=f"c{index}")
            for index in range(3)
        )
        completion = _Completion(_tool_turn(*calls))
        loop, executor, _, _, _ = _loop(completion=completion, executor=_Executor(failure))

        with pytest.raises(ModelProviderError) as raised:
            await _run_loop(loop, _run(), completion)

        assert raised.value.code == TOOL_LOOP_LIMIT
        assert len(executor.calls) == 3

    @pytest.mark.anyio
    async def test_a_success_resets_the_consecutive_failure_count(self) -> None:
        """A success clears the streak, so the limit counts *consecutive* failures only."""
        failure = ToolExecutionFailure(ToolFailureReason.ARGUMENTS_INVALID, "Bad arguments.")
        outcomes: list[Any] = [failure, ToolResult(text="ok"), failure, failure, failure]
        completion = _Completion(
            _tool_turn(_call("current_time", {"argument": "1"}, call_id="a")),
            _tool_turn(_call("current_time", {"argument": "2"}, call_id="b")),
            _tool_turn(
                _call("current_time", {"argument": "3"}, call_id="c"),
                _call("current_time", {"argument": "4"}, call_id="d"),
                _call("current_time", {"argument": "5"}, call_id="e"),
            ),
        )

        async def respond(descriptor: ToolDescriptor, arguments: dict[str, Any]) -> Any:
            item = outcomes.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

        loop, _, _, _, _ = _loop(completion=completion, executor=_Executor(respond))

        with pytest.raises(ModelProviderError) as raised:
            await _run_loop(loop, _run(), completion)

        # Five calls ran: the success at position two cleared the streak, so the limit was reached
        # by the third failure *after* it rather than by the fourth failure overall.
        assert raised.value.code == TOOL_LOOP_LIMIT
        assert outcomes == []

    @pytest.mark.anyio
    async def test_an_unclassified_exception_fails_the_run_and_is_not_a_tool_rejection(
        self,
    ) -> None:
        completion = _Completion(_tool_turn(_call("current_time", {"argument": "x"})))
        loop, executor, invocations, _, _ = _loop(
            completion=completion, executor=_Executor(RuntimeError("unexpected"))
        )

        with pytest.raises(ModelProviderError) as raised:
            await _run_loop(loop, _run(), completion)

        assert raised.value.code == INTERNAL_EXECUTION_ERROR
        assert len(executor.calls) == 1
        assert invocations.statuses() == [InvocationStatus.FAILED.value]

    @pytest.mark.anyio
    async def test_an_invalid_catalog_fails_before_any_provider_call(self) -> None:
        completion = _Completion(_final())
        loop, executor, _, _, _ = _loop(
            completion=completion,
            descriptors=tuple(
                replace(_descriptor(f"tool{index}"), description="d" * 30_000) for index in range(4)
            ),
        )

        with pytest.raises(ModelProviderError) as raised:
            await _run_loop(loop, _run(), completion)

        assert raised.value.code == TOOL_CATALOG_INVALID
        assert completion.requests == []
        assert executor.calls == []


class TestObservationBounding:
    @pytest.mark.anyio
    async def test_an_oversized_result_is_bounded_before_the_next_turn(self) -> None:
        limits = replace(TOOL_ENABLED_LIMITS, tool_result_max_bytes=1024)
        completion = _Completion(_tool_turn(_call("current_time", {"argument": "x"})), _final())
        loop, _, _, _, _ = _loop(
            completion=completion,
            executor=_Executor(ToolResult(text="short", structured={"big": "y" * 5000})),
        )

        await _run_loop(loop, _run(limits), completion)

        observation = completion.requests[1].turns[1]
        assert observation.structured is None
        assert observation.text == f"short{STRUCTURED_OMITTED_NOTE}"
        assert len(observation.text.encode("utf-8")) <= 1024

    @pytest.mark.anyio
    async def test_text_only_overflow_is_head_truncated(self) -> None:
        limits = replace(TOOL_ENABLED_LIMITS, tool_result_max_bytes=1024)
        completion = _Completion(_tool_turn(_call("current_time", {"argument": "x"})), _final())
        loop, _, _, _, _ = _loop(
            completion=completion, executor=_Executor(ToolResult(text="t" * 5000))
        )

        await _run_loop(loop, _run(limits), completion)

        observation = completion.requests[1].turns[1]
        assert observation.text.endswith(TEXT_TRUNCATED_NOTE)
        assert len(observation.text.encode("utf-8")) <= 1024


class TestDurableErrorMessage:
    """A declared failure must always be recordable, or it would be left looking ambiguous."""

    def test_a_bounded_message_is_recorded_verbatim(self) -> None:
        assert durable_error_message("arguments_invalid", "Bad arguments.") == "Bad arguments."

    @pytest.mark.parametrize("message", [None, "", "x" * 513])
    def test_an_unrecordable_message_falls_back_to_the_codes_static_text(
        self, message: str | None
    ) -> None:
        # Never an integrity error, and never a silently dropped classification.
        assert durable_error_message("arguments_invalid", message) == safe_error_message(
            "arguments_invalid"
        )

    @pytest.mark.anyio
    async def test_an_over_long_tool_message_still_closes_the_invocation_as_failed(self) -> None:
        failure = ToolExecutionFailure(ToolFailureReason.INTERNAL, "m" * 600)
        completion = _Completion(_tool_turn(_call("current_time", {"argument": "x"})), _final())
        loop, _, invocations, _, _ = _loop(completion=completion, executor=_Executor(failure))

        await _run_loop(loop, _run(), completion)

        # The failure is still `failed` rather than left `started`: the taxonomy is preserved.
        assert invocations.statuses() == [InvocationStatus.FAILED.value]


class TestRoutingGuards:
    @pytest.mark.anyio
    async def test_a_tool_free_run_never_enters_the_loop(self) -> None:
        completion = _Completion(_final())
        loop, _, _, _, _ = _loop(completion=completion)
        from nervos_core.domain.runs import STAGE_B_LIMITS

        with pytest.raises(ModelProviderError) as raised:
            await _run_loop(loop, _run(STAGE_B_LIMITS), completion)

        assert raised.value.code == MODEL_RESPONSE_INVALID
        assert completion.requests == []

    @pytest.mark.anyio
    async def test_a_run_that_is_not_running_never_enters_the_loop(self) -> None:
        completion = _Completion(_final())
        loop, _, _, _, _ = _loop(completion=completion)
        run = replace(_run(), status=RunStatus.CREATED, started_at=None)

        with pytest.raises(ModelProviderError) as raised:
            await _run_loop(loop, run, completion)

        assert raised.value.code == MODEL_RESPONSE_INVALID


def test_merge_usage_is_used_by_the_loop_and_exported() -> None:
    """The accumulator is public so its nullability rule is testable on its own."""
    assert merge_usage(ModelUsage(), ModelUsage(1, None, None)).input_tokens == 1


def test_the_descriptor_helper_uses_a_durable_identity() -> None:
    """A loop fixture must never hand the loop an id-less descriptor."""
    assert _descriptor("x").tool_definition_id > 0
    assert _descriptor("x").source_kind is ToolSourceKind.BUILTIN
