"""ADR 0034 at the mediator: one approval, one dispatch, and no retroactive authority.

These tests run against injected doubles and state the *decision* the mediator makes around
the gate. The durable approval transitions themselves are proved against a real schema in
``test_stage_h_security.py``; what is proved here is that the mediator never reaches an
executor without a consumed approval, and that a built-in tool keeps the unchanged Stage-D
path even when a gate is composed.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from nervos_core.application.approvals import (
    CONSUMED,
    CONSUMPTION_UNAVAILABLE,
    ApprovalAdmission,
    ApprovalGate,
    ApprovalRequest,
    ConsumeOutcome,
)
from nervos_core.application.model_completion import (
    INTERNAL_EXECUTION_ERROR,
    ModelProviderError,
)
from nervos_core.application.tool_invocation_mediator import ToolInvocationMediator
from nervos_core.application.tool_invocations import (
    ALLOWED_DECISION,
    ClaimHandle,
    InvocationRequest,
    RecordOutcome,
    RecordOutcomeKind,
    RefusalOutcome,
    RefusalOutcomeKind,
    ResultEnvelope,
    StartOutcome,
    StartOutcomeKind,
)
from nervos_core.application.tool_permissions import PermissionDecision, PermissionDenialReason
from nervos_core.application.tool_registry import ToolRegistry, ToolResult
from nervos_core.domain.runs import Run, RunLimits, RunStatus
from nervos_core.domain.tools import (
    RiskHints,
    ToolDescriptor,
    ToolSourceKind,
    ToolSourceRef,
)

NOW = datetime(2026, 10, 3, tzinfo=UTC)


@dataclass(frozen=True)
class _Claim:
    job_id: int = 1
    run_id: int = 1
    attempt_id: int = 1
    attempt_number: int = 1
    worker_id: str = "worker-1"
    claim_token: bytes = b"t" * 32
    lease_expires_at: datetime = NOW


CLAIM: ClaimHandle = _Claim()


def _run() -> Run:
    return Run(
        1,
        1,
        "nervos.chat",
        "2",
        "anthropic",
        "opaque/model",
        "search please",
        RunLimits(),
        RunStatus.RUNNING,
        NOW,
        started_at=NOW,
    )


def _descriptor(source_kind: ToolSourceKind) -> ToolDescriptor:
    name = "search" if source_kind is ToolSourceKind.MCP else "echo"
    return ToolDescriptor(
        tool_definition_id=5,
        upstream_name=name,
        model_name=f"mcp__{name}" if source_kind is ToolSourceKind.MCP else f"nervos__{name}",
        source_kind=source_kind,
        source_id=2 if source_kind is ToolSourceKind.MCP else None,
        display_name=name,
        description=f"The {name} tool.",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        output_schema=None,
        risk_hints=RiskHints(),
        fingerprint="0" * 64,
    )


@dataclass
class _Gate:
    """A gate double that returns exactly the branch a test is about."""

    admission: ApprovalAdmission = ApprovalAdmission.REQUESTED
    consumption: str = CONSUMED
    admitted: list[ApprovalRequest] = field(default_factory=list[ApprovalRequest])
    consumed: list[ApprovalRequest] = field(default_factory=list[ApprovalRequest])
    invocations: _Invocations | None = None
    poll_result: ApprovalAdmission = ApprovalAdmission.REFUSED

    def admit(self, request: ApprovalRequest, *, now: datetime) -> ApprovalAdmission:
        self.admitted.append(request)
        if self.admission is ApprovalAdmission.REQUESTED and len(self.admitted) > 1:
            return self.poll_result
        return self.admission

    def start(
        self, request: ApprovalRequest, *, claim: ClaimHandle, invocation_id: int, now: datetime
    ) -> StartOutcome:
        self.consumed.append(request)
        assert self.invocations is not None
        if self.consumption != CONSUMED:
            self.invocations.mark_denied(
                claim=claim,
                invocation_id=invocation_id,
                decision=PermissionDecision(
                    allowed=False, reason=PermissionDenialReason.HARD_POLICY_DENIED
                ),
                now=now,
            )
            return StartOutcome(StartOutcomeKind.DENIED, invocation_id)
        return self.invocations.mark_started(claim=claim, invocation_id=invocation_id, now=now)

    def abandon(self, request: ApprovalRequest, *, now: datetime) -> None:
        self.poll_result = ApprovalAdmission.REFUSED

    def consume(
        self, request: ApprovalRequest, *, consuming_attempt_id: int, now: datetime
    ) -> ConsumeOutcome:
        self.consumed.append(request)
        return ConsumeOutcome(self.consumption)


@dataclass
class _Row:
    invocation_id: int
    request: InvocationRequest
    status: str
    decision: str = ALLOWED_DECISION


class _Invocations:
    """The durable lifecycle's transitions, in memory, with no provider and no database."""

    def __init__(self) -> None:
        self.rows: list[_Row] = []
        self.refusals = 0

    def record_requested(
        self, *, claim: ClaimHandle, request: InvocationRequest, now: datetime
    ) -> RecordOutcome:
        row = _Row(len(self.rows) + 1, request, "requested")
        self.rows.append(row)
        return RecordOutcome(RecordOutcomeKind.REQUESTED, row.invocation_id)

    def mark_started(
        self, *, claim: ClaimHandle, invocation_id: int, now: datetime
    ) -> StartOutcome:
        self._row(invocation_id).status = "started"
        return StartOutcome(StartOutcomeKind.STARTED, invocation_id, ALLOWED_DECISION)

    def mark_denied(
        self, *, claim: ClaimHandle, invocation_id: int, decision: PermissionDecision, now: datetime
    ) -> bool:
        self._row(invocation_id).status = "denied"
        return True

    def mark_cancelled(self, *, claim: ClaimHandle, invocation_id: int, now: datetime) -> bool:
        self._row(invocation_id).status = "cancelled"
        return True

    def mark_succeeded(
        self, *, claim: ClaimHandle, invocation_id: int, envelope: ResultEnvelope, now: datetime
    ) -> bool:
        self._row(invocation_id).status = "succeeded"
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
        self._row(invocation_id).status = "failed"
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
        self._row(invocation_id).status = "ambiguous"
        return True

    def record_pre_dispatch_refusal(self, *, claim: ClaimHandle, now: datetime) -> RefusalOutcome:
        self.refusals += 1
        return RefusalOutcome(RefusalOutcomeKind.RECORDED)

    def _row(self, invocation_id: int) -> _Row:
        return next(row for row in self.rows if row.invocation_id == invocation_id)


class _Executor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, descriptor: ToolDescriptor, arguments: Any) -> ToolResult:
        self.calls.append(descriptor.upstream_name)
        return ToolResult(text="ok")


class _Authorizer:
    def check_permission(self, *, run_id: int, tool_definition_id: int) -> PermissionDecision:
        return PermissionDecision(allowed=True)


class _Source:
    """A tool source with no catalogue of its own: the test only needs the executor path."""

    async def list_tools(self) -> tuple[ToolDescriptor, ...]:
        return ()


class _Doubles:
    """The three injected collaborators the mediator owns, assembled for one test."""

    def __init__(self, authorizer: Any | None = None) -> None:
        self.executor = _Executor()
        self.invocations = _Invocations()
        self.registry = ToolRegistry()
        self.registry.register(
            source_ref=ToolSourceRef(ToolSourceKind.MCP, 2),
            source=_Source(),
            executor=self.executor,
        )
        self.registry.register(
            source_ref=ToolSourceRef(ToolSourceKind.BUILTIN, None),
            source=_Source(),
            executor=self.executor,
        )
        self.authorizer = authorizer or _Authorizer()

    def mediator(self, gate: ApprovalGate | None) -> ToolInvocationMediator:
        if isinstance(gate, _Gate):
            gate.invocations = self.invocations
        return ToolInvocationMediator(
            registry=self.registry,
            authorize=self.authorizer,
            invocations=self.invocations,
            clock=lambda: NOW,
            approvals=gate,
        )


def _mediator(gate: ApprovalGate | None) -> tuple[ToolInvocationMediator, _Executor, _Invocations]:
    doubles = _Doubles()
    return doubles.mediator(gate), doubles.executor, doubles.invocations


async def _invoke(mediator: ToolInvocationMediator, descriptor: ToolDescriptor) -> Any:
    return await mediator.invoke(
        descriptor=descriptor,
        arguments={"q": "annual report"},
        run=_run(),
        claim=CLAIM,
        tool_sequence=1,
        provider_call_id="call-1",
    )


def test_an_unapproved_external_action_never_reaches_the_executor() -> None:
    gate = _Gate(admission=ApprovalAdmission.REQUESTED)
    mediator, executor, invocations = _mediator(gate)

    outcome = asyncio.run(_invoke(mediator, _descriptor(ToolSourceKind.MCP)))

    assert outcome.denied is True
    assert executor.calls == []
    # The call is durable and closed as denied before the model is told anything about it.
    assert [row.status for row in invocations.rows] == ["denied"]
    assert gate.consumed == []


def test_an_approved_action_dispatches_exactly_once() -> None:
    gate = _Gate(admission=ApprovalAdmission.APPROVED, consumption=CONSUMED)
    mediator, executor, invocations = _mediator(gate)

    outcome = asyncio.run(_invoke(mediator, _descriptor(ToolSourceKind.MCP)))

    assert outcome.denied is False
    assert executor.calls == ["search"]
    assert len(gate.consumed) == 1
    assert [row.status for row in invocations.rows] == ["succeeded"]


def test_pending_approval_resumes_the_same_invocation_without_replay() -> None:
    gate = _Gate(admission=ApprovalAdmission.REQUESTED, poll_result=ApprovalAdmission.APPROVED)
    mediator, executor, invocations = _mediator(gate)
    outcome = asyncio.run(_invoke(mediator, _descriptor(ToolSourceKind.MCP)))
    assert not outcome.denied
    assert executor.calls == ["search"]
    assert len(invocations.rows) == 1
    assert invocations.rows[0].status == "succeeded"
    assert len(gate.consumed) == 1


def test_a_consumption_race_stops_the_dispatch() -> None:
    gate = _Gate(admission=ApprovalAdmission.APPROVED, consumption=CONSUMPTION_UNAVAILABLE)
    mediator, executor, invocations = _mediator(gate)

    outcome = asyncio.run(_invoke(mediator, _descriptor(ToolSourceKind.MCP)))

    assert outcome.denied is True
    assert executor.calls == []
    assert [row.status for row in invocations.rows] == ["denied"]


def test_an_unconfirmable_gate_fails_the_run_closed() -> None:
    gate = _Gate(admission=ApprovalAdmission.UNAVAILABLE)
    mediator, executor, invocations = _mediator(gate)

    with pytest.raises(ModelProviderError) as failure:
        asyncio.run(_invoke(mediator, _descriptor(ToolSourceKind.MCP)))

    assert failure.value.code == INTERNAL_EXECUTION_ERROR
    assert executor.calls == []
    assert [row.status for row in invocations.rows] == ["denied"]
    assert gate.consumed == []


def test_a_builtin_action_keeps_the_stage_d_path() -> None:
    gate = _Gate(admission=ApprovalAdmission.REQUESTED)
    mediator, executor, invocations = _mediator(gate)

    outcome = asyncio.run(_invoke(mediator, _descriptor(ToolSourceKind.BUILTIN)))

    assert outcome.denied is False
    assert executor.calls == ["echo"]
    assert gate.admitted == []
    assert gate.consumed == []
    assert [row.status for row in invocations.rows] == ["succeeded"]


def test_the_requested_approval_binds_this_exact_call() -> None:
    gate = _Gate(admission=ApprovalAdmission.REQUESTED)
    mediator, _, _ = _mediator(gate)
    descriptor = _descriptor(ToolSourceKind.MCP)

    asyncio.run(_invoke(mediator, descriptor))

    request = gate.admitted[0]
    assert request.run_id == 1
    assert request.job_id == 1
    assert request.attempt_id == 1
    assert request.tool_sequence == 1
    assert request.tool_definition_id == descriptor.tool_definition_id
    assert request.upstream_name == descriptor.upstream_name
    assert request.fingerprint == descriptor.fingerprint
    assert json.loads(json.dumps(dict(request.arguments))) == {"q": "annual report"}


def test_a_denied_permission_never_reaches_the_gate() -> None:
    """Stage D's own denial is still the first answer: the gate adds a condition, never one."""

    class _Denying(_Authorizer):
        def check_permission(self, *, run_id: int, tool_definition_id: int) -> PermissionDecision:
            return PermissionDecision(allowed=False, reason=PermissionDenialReason.NOT_GRANTED)

    doubles = _Doubles(_Denying())
    executor = doubles.executor
    gate = _Gate(admission=ApprovalAdmission.APPROVED)
    mediator = doubles.mediator(gate)

    outcome = asyncio.run(_invoke(mediator, _descriptor(ToolSourceKind.MCP)))

    assert outcome.denied is True
    assert executor.calls == []
    assert gate.admitted == []


_ = PermissionDenialReason  # re-exported for readers of this module's intent
