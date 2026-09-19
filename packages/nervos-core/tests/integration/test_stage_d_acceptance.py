"""D7 integrated Stage-D acceptance: the composition the repository never built in a test.

Every pre-D7 test constructed `RunExecutor` without a `tool_loop`, so the routing decision inside
`run_execution.py` -- which shape a Run takes, and what happens when a tool-enabled Run arrives with
no claim to fence its writes on -- was never exercised. These journeys go through
`JobExecutionService` and `RunExecutor` so that seam is crossed by construction, and they assert
what the engine durably committed rather than what a helper returned.

Journey A is the backward-compatibility guard: `nervos.chat@1` must still be exactly the Stage B/C
execution it always was. Journey B is the first integrated Think -> Act -> Observe path through the
real service. The rest carry D2's authority, D4's failure semantics, and D6's audit through that
same composition.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
from d6_support import LATER, ScriptedCompletion, cancel
from nervos_core.application.job_execution import ClaimedAttempt
from nervos_core.application.model_completion import (
    EXECUTION_OUTCOME_AMBIGUOUS,
    INTERNAL_EXECUTION_ERROR,
    TOOL_LOOP_LIMIT,
    ModelRequest,
    ModelResponse,
)
from nervos_core.application.run_cancellation import CancellationOutcome
from nervos_core.application.run_execution import RunExecutor
from nervos_core.application.tool_invocations import (
    InvocationRequest,
    InvocationStatus,
    RecordOutcomeKind,
)
from nervos_core.application.tool_permissions import PermissionDecision
from nervos_core.application.trusted_chat import create_builtin_handler_registry
from nervos_core.domain.jobs import RunEventType
from nervos_core.domain.runs import STAGE_B_LIMITS, TOOL_ENABLED_LIMITS, Run, RunStatus
from nervos_core.domain.tools import ToolDescriptor
from nervos_core.infrastructure.database.agents import SqlAlchemyAgentPersistence
from nervos_core.infrastructure.database.engine import create_sqlite_engine
from nervos_core.infrastructure.database.jobs import SqlAlchemyJobExecutionPersistence
from nervos_core.infrastructure.database.tool_invocations import (
    SqlAlchemyToolInvocationPersistence,
)
from sqlalchemy.orm import sessionmaker
from stage_d_support import (
    NOW,
    Rig,
    build_execution,
    build_rig,
    claim_only,
    descriptor_named,
    event_sequences,
    event_types,
    execute_run,
    final_turn,
    grant,
    invitations,
    job_and_attempt_counts,
    revoke,
    run_row,
    submit,
    tool_events,
    tool_turn,
)

TOOL_DENIED_CODE = "tool_denied"


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    return build_rig(tmp_path, monkeypatch, name="d7-core.db")


class RevokingCompletion(ScriptedCompletion):
    """Revoke a grant mid-loop, so the *second* live check disagrees with catalog assembly.

    This is the only way to reach the requested-then-denied shape honestly. Revoking before the Run
    is claimed would remove the tool from the catalog and produce a different, pre-dispatch refusal
    instead, which is a different guarantee.
    """

    def __init__(self, *responses: ModelResponse, rig: Rig, descriptor: ToolDescriptor) -> None:
        super().__init__(*responses)
        self._rig = rig
        self._descriptor = descriptor
        self._revoked = False

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not self._revoked:
            self._revoked = True
            revoke(self._rig, descriptor=self._descriptor)
        return await super().complete(request)


def test_journey_a_a_tool_free_run_is_still_the_stage_b_execution(rig: Rig) -> None:
    """`nervos.chat@1` takes the handler path and records no tool anything.

    This is the regression that matters most: D1-D6 added a whole tool layer beside the original
    one-shot execution, and the only way to know they did not disturb it is to run a tool-free Run
    through the same composition that now carries a tool loop.
    """
    run_id = submit(rig, limits=STAGE_B_LIMITS)
    stage = build_execution(rig, completion=ScriptedCompletion(final_turn("plain answer")))
    outcome = asyncio.run(execute_run(stage, run_id))

    assert outcome is not None
    assert outcome.status == "succeeded"
    assert outcome.output_text == "plain answer"
    # The tool layer was reachable and stayed untouched: no invocation, no tool event.
    assert invitations(rig) == []
    assert tool_events(rig, run_id) == []
    assert job_and_attempt_counts(rig, run_id) == (1, 1, 1)
    assert run_row(rig, run_id)["status"] == RunStatus.SUCCEEDED.value


def test_journey_b_a_builtin_tool_runs_through_run_executor_and_settles_the_run(rig: Rig) -> None:
    """One grant, one tool call, one observation, one final answer -- through the real service.

    The proof this adds over `test_tool_loop_execution.py` is the seam: the Run's snapshot alone
    decides that it is tool-enabled, `RunExecutor` routes it to the loop, and the loop's writes are
    fenced on the claim the service took. Nothing here calls `ToolLoop.run` directly.
    """
    descriptor = descriptor_named(rig, "current_time")
    grant(rig, descriptor=descriptor)
    run_id = submit(rig, limits=TOOL_ENABLED_LIMITS)

    completion = ScriptedCompletion(
        tool_turn(descriptor.model_name, '{"timezone":"UTC"}', call_id="call-1"),
        final_turn("the time is known"),
    )
    stage = build_execution(rig, completion=completion)
    outcome = asyncio.run(execute_run(stage, run_id))

    assert outcome is not None and outcome.status == "succeeded"
    assert outcome.output_text == "the time is known"
    # The model was consulted exactly twice: once to ask, once to conclude.
    assert len(completion.requests) == 2

    rows = invitations(rig)
    assert len(rows) == 1
    assert rows[0]["status"] == InvocationStatus.SUCCEEDED.value
    assert rows[0]["tool_definition_id"] == descriptor.tool_definition_id

    # The three frozen events, in order, each naming the invocation the loop recorded.
    observed = tool_events(rig, run_id)
    assert [event[0] for event in observed] == [
        RunEventType.TOOL_REQUESTED.value,
        RunEventType.TOOL_STARTED.value,
        RunEventType.TOOL_SUCCEEDED.value,
    ]
    assert {event[1] for event in observed} == {rows[0]["id"]}

    # A whole multi-turn loop is still one Run, one Job and one Attempt.
    assert job_and_attempt_counts(rig, run_id) == (1, 1, 1)
    assert run_row(rig, run_id)["status"] == RunStatus.SUCCEEDED.value


def test_the_tool_enabled_path_and_the_handler_path_do_not_disagree(rig: Rig) -> None:
    """A Run whose snapshot allows no tools never reaches the loop, even with a granted tool.

    `RunExecutor` chooses the shape from the Run's own frozen limits, not from what the registry
    happens to hold. A granted, catalogued tool must therefore still be invisible to a tool-free
    Run, which is the composition-level version of the D2 cutoff guarantee.
    """
    descriptor = descriptor_named(rig, "current_time")
    grant(rig, descriptor=descriptor)
    run_id = submit(rig, limits=STAGE_B_LIMITS)
    stage = build_execution(rig, completion=ScriptedCompletion(final_turn("plain again")))
    outcome = asyncio.run(execute_run(stage, run_id))

    assert outcome is not None and outcome.status == "succeeded"
    assert invitations(rig) == []
    assert tool_events(rig, run_id) == []
    # The unchanged Stage B/C timeline: acceptance, claim, the execution-start boundary, success.
    assert event_types(rig, run_id) == [
        "run.created",
        "run.queued",
        "attempt.claimed",
        "attempt.started",
        "run.succeeded",
    ]


def test_a_tool_enabled_run_with_no_claim_fails_closed_rather_than_guessing(rig: Rig) -> None:
    """The `RunExecutor` fail-closed branch: a tool-enabled Run with nothing to fence on.

    A tool loop writes durable state under a claim, so a tool-enabled Run reaching the executor
    without one is a routing defect. The safe answer is a normalized internal failure, never a
    silent fall back to the tool-free path -- which would execute the Run while quietly dropping
    the capability it was admitted for.
    """
    descriptor = descriptor_named(rig, "current_time")
    grant(rig, descriptor=descriptor)
    run_id = submit(rig, limits=TOOL_ENABLED_LIMITS)
    claimant = claim_only(rig, run_id)
    run = stage_run(rig, run_id)

    executor = RunExecutor(create_builtin_handler_registry(), tool_loop=build_execution(rig).loop)
    completion = ScriptedCompletion(final_turn("should never be reached"))
    outcome = asyncio.run(executor.execute(run, completion, None))

    assert completion.requests == []
    assert outcome.status == "failed"
    assert outcome.error_code == INTERNAL_EXECUTION_ERROR
    # And nothing was dispatched, even though a claim existed elsewhere.
    assert claimant.attempt_id > 0
    assert invitations(rig) == []


def stage_run(rig: Rig, run_id: int) -> Run:
    """Load a Run through the execution persistence the service itself uses."""
    return SqlAlchemyJobExecutionPersistence(rig.engine).load_run(run_id)


def test_journey_f_a_tool_with_no_grant_is_refused_before_anything_runs(rig: Rig) -> None:
    """Default deny at composition level: the tool exists durably and is still not offered.

    The model names a tool that was never granted, which is the hostile case: the catalog decides
    what exists, and the refusal is recorded with no invocation, because no call was ever requested.
    """
    descriptor = descriptor_named(rig, "current_time")
    run_id = submit(rig, limits=TOOL_ENABLED_LIMITS)

    completion = ScriptedCompletion(
        tool_turn(descriptor.model_name, '{"timezone":"UTC"}', call_id="call-1"),
        final_turn("refused and moved on"),
    )
    stage = build_execution(rig, completion=completion)
    outcome = asyncio.run(execute_run(stage, run_id))

    assert outcome is not None and outcome.status == "succeeded"
    # No grant, so no invocation row exists at all.
    assert invitations(rig) == []

    observed = tool_events(rig, run_id)
    assert [event[0] for event in observed] == [RunEventType.TOOL_DENIED.value]
    assert observed[0][1] is None, "a pre-dispatch refusal must carry no invocation id"
    assert observed[0][2] == TOOL_DENIED_CODE
    # The generic public code never leaks which internal reason applied.
    assert "not_granted" not in (observed[0][2] or "")


@pytest.mark.parametrize(
    ("tool", "label", "arguments_json"),
    [
        ("current_time", "malformed_json", '{"timezone":'),
        ("current_time", "not_an_object", "[]"),
        ("current_time", "wrong_type", '{"timezone": 5}'),
        # `current_time` defaults its `timezone`, so `{}` is genuinely valid there and dispatching
        # it is correct. `calculate` requires `expression`, which is what makes this a real
        # missing-required case rather than an argument the tool happens to accept.
        ("calculate", "missing_required", "{}"),
    ],
)
def test_journey_p_an_unusable_request_is_refused_before_any_invocation_exists(
    rig: Rig, tool: str, label: str, arguments_json: str
) -> None:
    """A tool that *is* catalogued can still be refused, and the refusal leaves no invocation.

    This is the other half of default deny. The tool is granted and present in the assembled
    catalog, so the refusal cannot come from membership -- it comes from the request being unusable.
    No call was requested, so no `ToolInvocation` row may exist, and the durable audit must commit
    *before* the model is told anything about it. All four shapes collapse to one generic public
    code, which is what keeps the internal reason out of the timeline.
    """
    descriptor = descriptor_named(rig, tool)
    grant(rig, descriptor=descriptor)
    run_id = submit(rig, limits=TOOL_ENABLED_LIMITS)

    completion = ScriptedCompletion(
        tool_turn(descriptor.model_name, arguments_json, call_id="call-1"),
        final_turn(f"refused {label} and moved on"),
    )
    stage = build_execution(rig, completion=completion)
    outcome = asyncio.run(execute_run(stage, run_id))

    # The refusal is a known outcome, so the Run may legally continue to a final answer.
    assert outcome is not None and outcome.status == "succeeded"
    assert invitations(rig) == [], f"{label} created an invocation row"

    observed = tool_events(rig, run_id)
    assert [event[0] for event in observed] == [RunEventType.TOOL_DENIED.value], label
    assert observed[0][1] is None, f"{label} named an invocation it never created"
    assert observed[0][2] == TOOL_DENIED_CODE, label


def test_journey_g_a_grant_created_after_submission_stays_invisible_to_that_run(rig: Rig) -> None:
    """The grant cutoff, proved across two Runs through the real service.

    Run 1 is submitted with no grants at all, so its snapshotted cutoff is 0. The grant is created
    afterwards. Run 1 must never see it; Run 2, submitted after the grant, must.
    """
    descriptor = descriptor_named(rig, "current_time")
    early_run = submit(rig, limits=TOOL_ENABLED_LIMITS)
    grant(rig, descriptor=descriptor)
    late_run = submit(rig, limits=TOOL_ENABLED_LIMITS)

    early_completion = ScriptedCompletion(
        tool_turn(descriptor.model_name, '{"timezone":"UTC"}', call_id="call-1"),
        final_turn("early could not use it"),
    )
    early = build_execution(rig, completion=early_completion)
    asyncio.run(execute_run(early, early_run))

    assert invitations(rig) == [], "a grant created after submission reached an earlier Run"
    assert [event[0] for event in tool_events(rig, early_run)] == [RunEventType.TOOL_DENIED.value]

    late_completion = ScriptedCompletion(
        tool_turn(descriptor.model_name, '{"timezone":"UTC"}', call_id="call-1"),
        final_turn("late used it"),
    )
    late = build_execution(rig, completion=late_completion)
    outcome = asyncio.run(execute_run(late, late_run))

    assert outcome is not None and outcome.status == "succeeded"
    rows = invitations(rig)
    assert len(rows) == 1 and rows[0]["status"] == InvocationStatus.SUCCEEDED.value


def test_journey_h_a_grant_revoked_before_start_denies_the_requested_call(rig: Rig) -> None:
    """Requested, then denied, with no start and no executor call.

    The revoke happens during the model turn, so catalog assembly already saw a granted tool and the
    call is genuinely *requested*. The in-transaction live check then disagrees, which is exactly
    the second-check guarantee D2 promises -- and the denial is a known outcome, so the Run may
    legally continue.
    """
    descriptor = descriptor_named(rig, "current_time")
    grant(rig, descriptor=descriptor)
    run_id = submit(rig, limits=TOOL_ENABLED_LIMITS)

    completion = RevokingCompletion(
        tool_turn(descriptor.model_name, '{"timezone":"UTC"}', call_id="call-1"),
        final_turn("denied and moved on"),
        rig=rig,
        descriptor=descriptor,
    )
    stage = build_execution(rig, completion=completion)
    asyncio.run(execute_run(stage, run_id))

    rows = invitations(rig)
    assert len(rows) == 1
    assert rows[0]["status"] == InvocationStatus.DENIED.value
    assert rows[0]["started_at"] is None, "a denied call must never cross the start boundary"

    observed = tool_events(rig, run_id)
    assert [event[0] for event in observed] == [
        RunEventType.TOOL_REQUESTED.value,
        RunEventType.TOOL_DENIED.value,
    ]
    # The denial names the invocation it refused, unlike a pre-dispatch refusal.
    assert observed[1][1] == rows[0]["id"]
    assert observed[1][2] == TOOL_DENIED_CODE
    assert run_row(rig, run_id)["status"] == RunStatus.SUCCEEDED.value


def test_journey_q_the_consecutive_failure_cap_ends_a_run_that_keeps_failing(rig: Rig) -> None:
    """Three consecutive safe failures stop the loop, and the cap is the Run's own limit.

    Every failure the model can see counts the same way, so a model that keeps asking for an
    unusable tool is bounded rather than allowed to spend the whole model budget retrying.
    """
    descriptor = descriptor_named(rig, "calculate")
    grant(rig, descriptor=descriptor)
    run_id = submit(rig, limits=TOOL_ENABLED_LIMITS)

    # A malformed expression is the tool's own classified failure, never a transport error.
    bad = '{"expression":"1 +* 2"}'
    completion = ScriptedCompletion(
        tool_turn(descriptor.model_name, bad, call_id="call-1"),
        tool_turn(descriptor.model_name, bad, call_id="call-2"),
        tool_turn(descriptor.model_name, bad, call_id="call-3"),
        final_turn("never reached"),
    )
    stage = build_execution(rig, completion=completion)
    outcome = asyncio.run(execute_run(stage, run_id))

    assert outcome is not None and outcome.status == "failed"
    assert outcome.error_code == TOOL_LOOP_LIMIT
    rows = invitations(rig)
    assert [row["status"] for row in rows] == [InvocationStatus.FAILED.value] * 3
    # The cap ended the loop, so the concluding turn never happened.
    assert len(completion.requests) == 3


def test_journey_r_a_long_loop_stays_one_run_one_job_and_one_attempt(rig: Rig) -> None:
    """Several turns and several calls add no durable obligation of their own.

    This is the composition-level counterpart of the D4 durable-shape suite: the counts must be
    independent of how many times the model was consulted.
    """
    first = descriptor_named(rig, "current_time")
    second = descriptor_named(rig, "calculate")
    grant(rig, descriptor=first)
    grant(rig, descriptor=second)
    run_id = submit(rig, limits=TOOL_ENABLED_LIMITS)

    completion = ScriptedCompletion(
        tool_turn(first.model_name, '{"timezone":"UTC"}', call_id="call-1"),
        tool_turn(second.model_name, '{"expression":"2*3"}', call_id="call-2"),
        tool_turn(first.model_name, '{"timezone":"UTC"}', call_id="call-3"),
        final_turn("all three ran"),
    )
    stage = build_execution(rig, completion=completion)
    outcome = asyncio.run(execute_run(stage, run_id))

    assert outcome is not None and outcome.status == "succeeded"
    assert len(invitations(rig)) == 3
    assert job_and_attempt_counts(rig, run_id) == (1, 1, 1)
    # Sequences are dense and ascending: audit amplification stays bounded to the calls made.
    assert event_sequences(rig, run_id) == list(range(1, len(event_sequences(rig, run_id)) + 1))


def test_journey_x_durable_tool_truth_survives_an_engine_restart(rig: Rig, tmp_path: Path) -> None:
    """Everything a tool-enabled Run recorded survives engine teardown and recomposition.

    The claim is deliberately narrow, and the narrowness is the point: no correctness may live in a
    Python object. This disposes the engine and reopens the same file, then compares the durable
    facts -- it does not spawn a second interpreter and does not claim to.
    """
    descriptor = descriptor_named(rig, "current_time")
    grant(rig, descriptor=descriptor)
    run_id = submit(rig, limits=TOOL_ENABLED_LIMITS)
    stage = build_execution(
        rig,
        completion=ScriptedCompletion(
            tool_turn(descriptor.model_name, '{"timezone":"UTC"}', call_id="call-1"),
            final_turn("restart-proof"),
        ),
    )
    asyncio.run(execute_run(stage, run_id))

    snapshot = {
        "run": run_row(rig, run_id),
        "events": tool_events(rig, run_id),
        "sequences": event_sequences(rig, run_id),
        "invocations": [dict(row) for row in invitations(rig)],
        "counts": job_and_attempt_counts(rig, run_id),
    }
    rig.engine.dispose()

    reopened = create_sqlite_engine(tmp_path / "d7-core.db")
    try:
        from d6_support import Rig as RigType

        fresh = RigType(engine=reopened, descriptors=rig.descriptors, instance_id=rig.instance_id)
        assert run_row(fresh, run_id) == snapshot["run"]
        assert tool_events(fresh, run_id) == snapshot["events"]
        assert event_sequences(fresh, run_id) == snapshot["sequences"]
        assert [dict(row) for row in invitations(fresh)] == snapshot["invocations"]
        assert job_and_attempt_counts(fresh, run_id) == snapshot["counts"]
    finally:
        reopened.dispose()


def test_journey_aa_the_public_timeline_agrees_with_durable_tool_truth(rig: Rig) -> None:
    """The C7 read path -- not raw SQL -- reports the tool lifecycle, and leaks nothing.

    Every pre-D7 cross-subsystem tool test asserted on `run_events` with direct SQL, so the public
    projection of a tool-using Run had never been read end to end. This goes through the same
    reader the API uses, which is what makes it a projection test rather than a table test.
    """
    descriptor = descriptor_named(rig, "current_time")
    grant(rig, descriptor=descriptor)
    run_id = submit(rig, limits=TOOL_ENABLED_LIMITS)
    stage = build_execution(
        rig,
        completion=ScriptedCompletion(
            tool_turn(descriptor.model_name, '{"timezone":"UTC"}', call_id="call-1"),
            final_turn("timeline-proof"),
        ),
    )
    asyncio.run(execute_run(stage, run_id))

    reader = SqlAlchemyAgentPersistence(sessionmaker(bind=rig.engine))
    page = reader.list_run_events(run_id, 0, 200)
    tool_page = [event for event in page if str(event.event_type).startswith("tool.")]

    assert [str(event.event_type) for event in tool_page] == [
        "tool.requested",
        "tool.started",
        "tool.succeeded",
    ]
    # Ascending sequence, and every tool event names the invocation it belongs to.
    assert [event.sequence for event in page] == sorted(event.sequence for event in page)
    invocation_id = invitations(rig)[0]["id"]
    assert {event.tool_invocation_id for event in tool_page} == {invocation_id}

    # Nothing argument-derived, provider-derived or credential-shaped reaches the projection.
    published = " ".join(
        str(value)
        for event in page
        for value in (event.event_type, event.code, event.message)
        if value is not None
    )
    assert "timezone" not in published
    assert "UTC" not in published
    assert EXECUTION_OUTCOME_AMBIGUOUS not in published
    # The stored tool result stays in the invocation table; it is never echoed into an event.
    stored = invitations(rig)[0]
    for column in ("arguments_json", "result_text", "output_text"):
        value = stored.get(column)
        if isinstance(value, str) and value:
            assert value not in published


# ---------------------------------------------------------------------------------------
# Cancellation, from both sides of the dispatch boundary
# ---------------------------------------------------------------------------------------


class CancellingCompletion(ScriptedCompletion):
    """Cancel the Run while its first model turn is being answered."""

    def __init__(self, *responses: ModelResponse, cancel_run: Callable[[], object]) -> None:
        super().__init__(*responses)
        self._cancel_run = cancel_run
        self._cancelled = False

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not self._cancelled:
            self._cancelled = True
            self._cancel_run()
        return await super().complete(request)


def test_journey_m_cancellation_winning_while_the_model_turn_is_in_flight_stops_the_loop(
    rig: Rig,
) -> None:
    """The revocation lands mid-turn, so the loop's next write is fenced and no call runs.

    The composed version of the D6 guarantee: the cancellation is issued by the owner's path while
    the Attempt is executing, and the service must report a cancelled Run rather than persisting a
    result it no longer has the authority to write.
    """
    descriptor = descriptor_named(rig, "current_time")
    grant(rig, descriptor=descriptor)
    run_id = submit(rig, limits=TOOL_ENABLED_LIMITS)

    completion = CancellingCompletion(
        tool_turn(descriptor.model_name, '{"timezone":"UTC"}', call_id="call-1"),
        final_turn("never reached"),
        cancel_run=lambda: cancel(rig, run_id, now=LATER),
    )
    stage = build_execution(rig, completion=completion)
    asyncio.run(execute_run(stage, run_id))

    # Nothing was dispatched and nothing was requested, so the tool timeline is empty.
    assert invitations(rig) == []
    assert tool_events(rig, run_id) == []
    # Only the two frozen cancellation events, and the invocation is created by the Run's own end.
    assert event_types(rig, run_id)[-2:] == ["cancellation.requested", "run.cancelled"]
    assert run_row(rig, run_id)["status"] == RunStatus.CANCELLED.value


def test_journey_m2_a_requested_call_cancelled_before_dispatch_is_never_dispatched(
    rig: Rig,
) -> None:
    """A `requested` row that provably never crossed the start boundary closes as `cancelled`.

    This is the row the timeline must stay silent about. The six frozen tool event types contain no
    `tool.cancelled`, so the only durable trace of a cancelled-before-dispatch call is the
    invocation's own status -- and `run.cancelled` remains the single cancellation truth. The
    invoked claim is the one the execution service issues, so the row's foreign keys are genuine.
    """
    descriptor = descriptor_named(rig, "current_time")
    grant(rig, descriptor=descriptor)
    run_id = submit(rig, limits=TOOL_ENABLED_LIMITS)
    handle = claim_only(rig, run_id)
    assert SqlAlchemyJobExecutionPersistence(rig.engine).start_attempt(handle, now=NOW) is True

    invocations_port = SqlAlchemyToolInvocationPersistence(rig.engine)
    recorded = invocations_port.record_requested(
        claim=handle,
        request=_invocation_request(descriptor, handle),
        now=NOW,
    )
    assert recorded.kind is RecordOutcomeKind.REQUESTED
    assert recorded.invocation_id is not None

    assert cancel(rig, run_id, now=LATER) is CancellationOutcome.CANCELLED

    row = invitations(rig)[0]
    assert row["id"] == recorded.invocation_id
    assert row["status"] == InvocationStatus.CANCELLED.value
    # The schema itself forbids a cancelled call from claiming it started.
    assert row["started_at"] is None
    assert row["finished_at"] is not None

    observed = tool_events(rig, run_id)
    assert observed == [("tool.requested", recorded.invocation_id, None)]
    assert "tool.ambiguous" not in event_types(rig, run_id)
    assert event_types(rig, run_id)[-2:] == ["cancellation.requested", "run.cancelled"]
    assert run_row(rig, run_id)["status"] == RunStatus.CANCELLED.value


def _invocation_request(descriptor: ToolDescriptor, handle: ClaimedAttempt) -> InvocationRequest:
    """Build one request bound to a real claim, so the recorded row's foreign keys are genuine."""
    return InvocationRequest(
        run_id=handle.run_id,
        job_id=handle.job_id,
        attempt_id=handle.attempt_id,
        tool_sequence=1,
        tool_definition_id=descriptor.tool_definition_id,
        source_kind=descriptor.source_kind,
        source_id=descriptor.source_id,
        upstream_name=descriptor.upstream_name,
        model_name=descriptor.model_name,
        definition_fingerprint=descriptor.fingerprint,
        provider_call_id="call-1",
        permission_decision=PermissionDecision(allowed=True),
        arguments={"timezone": "UTC"},
    )
