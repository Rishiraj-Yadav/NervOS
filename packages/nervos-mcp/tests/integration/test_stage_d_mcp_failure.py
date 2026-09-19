"""D7 MCP-side failure acceptance: the fate of a *remote* tool call the Worker cannot finish.

Every journey here crosses the seam no pre-D7 test crossed -- `JobExecutionService.execute` handing
a tool-enabled Run to `RunExecutor`, which routes it into `ToolLoop` -- and does so over a live fake
MCP server. The assertions are the durable truth (`tool_invocations`, `run_events`, the job/attempt
rows) and the server's own ledger, because the question these paths answer is whether an external
effect happened once or twice.

Four facts drive the whole suite, all stated by the shipped code rather than invented here:

* a dispatched call is a whole-Attempt replay boundary, so a rate limit that is otherwise safe to
  retry settles terminally instead of re-running the Attempt;
* a lease that expires after dispatch closes the call `ambiguous`, never `failed`, and never
  re-dispatches it;
* an owner cancellation after dispatch closes the call `ambiguous` and fences every late result;
* a tool deadline is ambiguity, not failure, because the deadline says nothing about the effect.

Time is injected everywhere: a held call is released by the test, a lease expires by reading a later
`now`, and no test sleeps for a backoff or a lease duration.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from nervos_core.application.job_execution import CancellationOutcome
from nervos_core.application.model_completion import (
    EXECUTION_CANCELLED,
    EXECUTION_OUTCOME_AMBIGUOUS,
    MODEL_RATE_LIMITED,
    TOOL_OUTCOME_UNKNOWN,
    ModelProviderError,
    ModelUsage,
)
from nervos_core.application.tool_invocations import InvocationStatus
from nervos_core.domain.runs import RunLimits, RunStatus

from ..support.fake_mcp_server import WRITE_DOCUMENT
from ..support.loop_harness import (
    LATER,
    RecordingSleeper,
    ScriptedCompletion,
    final_turn,
    tool_call,
    tool_turn,
)
from .stage_d_mcp_failure_support import (
    FAST_HEARTBEAT,
    ManualClock,
    attempt_state,
    await_until,
    build_execution,
    cancel_run,
    claim_only,
    event_rows,
    event_types,
    invocation_row,
    invocation_states,
    job_row,
    mcp_world,
    reclaim,
    run_row,
    submit_run,
    tool_events,
)

WRITE_ARGUMENTS = {"path": "a.txt", "content": "one"}
# One second is the smallest per-tool deadline the domain permits, so J-O drives the engine's real
# bound with the real `asyncio.timeout`, not a shortened stand-in.
TOOL_TIMEOUT_LIMITS = RunLimits(max_model_calls=8, max_tool_calls=8, tool_timeout_ms=1000)


@pytest.mark.anyio
async def test_journey_k_a_rate_limited_turn_after_a_remote_write_never_replays_the_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dispatched remote write makes the whole Attempt unreplayable, however safe the model error.

    The write lands once; the next model turn is rate-limited; the in-Attempt ladder exhausts; the
    Run is settled `failed` with the model's own rate-limit semantics, and C4's whole-Attempt guard
    refuses the retry a rate limit would otherwise have earned. The ledger count is the proof: an
    external effect happened exactly once.
    """
    sleeper = RecordingSleeper()
    async with mcp_world(tmp_path, monkeypatch, sleep=sleeper) as world:
        write = world.descriptor(WRITE_DOCUMENT)
        run_id = submit_run(world.engine, instance_id=world.instance_id)
        claim = claim_only(world.engine, run_id)
        completion = ScriptedCompletion(
            tool_turn(tool_call("call-1", write.model_name, WRITE_ARGUMENTS)),
            beyond=ModelProviderError(MODEL_RATE_LIMITED, usage=ModelUsage()),
        )
        execution = build_execution(world.rig, completion=completion, sleeper=sleeper)

        outcome = await execution.execute(claim)

        assert outcome is not None and outcome.status == "failed"
        assert outcome.error_code == MODEL_RATE_LIMITED
        # The Layer-1 ladder ran to exhaustion at its frozen delays, then raised the model's code.
        assert sleeper.delays == [1.0, 2.0, 4.0]
        # The remote effect landed exactly once: the retry ladder never re-dispatched the call.
        assert world.server.ledger.counts[WRITE_DOCUMENT] == 1
        assert invocation_states(world.engine) == [InvocationStatus.SUCCEEDED.value]

        # C4's guard saw the dispatched call, so no second Attempt exists and no retry was queued.
        assert job_row(world.engine, run_id)["attempt_count"] == 1
        types = event_types(world.engine, run_id)
        assert types.count("tool.started") == 1
        assert types.count("tool.succeeded") == 1
        assert "retry.scheduled" not in types
        assert "attempt.failed" in types

        state = run_row(world.engine, run_id)
        assert state["status"] == RunStatus.FAILED.value
        assert state["error_code"] == MODEL_RATE_LIMITED


@pytest.mark.anyio
async def test_journey_l_an_expired_lease_after_a_remote_write_reconciles_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real C3 recovery path reconciles a real MCP-dispatched call the Worker abandoned.

    `tool.started` is committed, the write reaches the server, and the Worker disappears before the
    `succeeded` write. The lease then expires and the shipped reclamation runs: the call goes
    `started -> ambiguous`, one `tool.ambiguous` follows the recovery facts, the Run fails
    `execution_outcome_ambiguous`, and the ledger stays at exactly one. Reclamation of a *remote*
    dispatch was the one recovery path no test had reached.
    """
    gate = asyncio.Event()
    async with mcp_world(tmp_path, monkeypatch, gate=gate) as world:
        write = world.descriptor(WRITE_DOCUMENT)
        run_id = submit_run(world.engine, instance_id=world.instance_id)
        claim = claim_only(world.engine, run_id)
        completion = ScriptedCompletion(
            tool_turn(tool_call("call-1", write.model_name, WRITE_ARGUMENTS)),
            final_turn("unreachable"),
        )
        execution = build_execution(world.rig, completion=completion)

        worker = asyncio.create_task(execution.execute(claim))
        # Wait for a committed start byte AND a landed remote effect: the exact durable state a
        # process that died mid-dispatch leaves behind.
        await await_until(
            lambda: (
                invocation_states(world.engine) == [InvocationStatus.STARTED.value]
                and world.server.ledger.counts.get(WRITE_DOCUMENT, 0) == 1
            )
        )
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        gate.set()

        assert reclaim(world.engine, now=LATER) is not None

        row = invocation_row(world.engine, status=InvocationStatus.AMBIGUOUS)
        assert row["error_code"] == TOOL_OUTCOME_UNKNOWN
        # It crossed the start boundary and may have landed: `started`->`ambiguous` is the shape.
        assert row["started_at"] is not None
        # Never `failed`: `failed` would claim a conclusion nobody observed.
        assert invocation_states(world.engine) == [InvocationStatus.AMBIGUOUS.value]
        ambiguous = [
            event for event in tool_events(world.engine, run_id) if event[0] == "tool.ambiguous"
        ]
        assert len(ambiguous) == 1
        assert ambiguous[0][1] == row["id"]
        assert ambiguous[0][2] == TOOL_OUTCOME_UNKNOWN
        assert event_types(world.engine, run_id)[-4:] == [
            "attempt.expired",
            "recovery.ambiguous",
            "tool.ambiguous",
            "run.failed",
        ]
        state = run_row(world.engine, run_id)
        assert state["status"] == RunStatus.FAILED.value
        assert state["error_code"] == EXECUTION_OUTCOME_AMBIGUOUS
        # No replay: the one remote effect stands, and recovery dispatched nothing.
        assert world.server.ledger.counts[WRITE_DOCUMENT] == 1
        assert job_row(world.engine, run_id)["attempt_count"] == 1


@pytest.mark.anyio
async def test_journey_n_a_cancellation_after_dispatch_closes_the_call_and_fences_the_late_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner cancels while the remote work is still in flight; the late result cannot land.

    `tool.started` is committed and the server is holding the call, so the cancellation wins against
    a call that may still have an effect. The call is closed `ambiguous` between the two Run events,
    the returning result is fenced by the revoked claim, and no observation or second model turn is
    ever produced.
    """
    gate = asyncio.Event()
    async with mcp_world(tmp_path, monkeypatch, gate=gate) as world:
        write = world.descriptor(WRITE_DOCUMENT)
        run_id = submit_run(world.engine, instance_id=world.instance_id)
        claim = claim_only(world.engine, run_id)
        completion = ScriptedCompletion(
            tool_turn(tool_call("call-1", write.model_name, WRITE_ARGUMENTS)),
            final_turn("never reached"),
        )
        execution = build_execution(world.rig, completion=completion)

        worker = asyncio.create_task(execution.execute(claim))
        await await_until(
            lambda: (
                invocation_states(world.engine) == [InvocationStatus.STARTED.value]
                and world.server.ledger.counts.get(WRITE_DOCUMENT, 0) == 1
            )
        )

        assert cancel_run(world.engine, run_id, now=LATER) is CancellationOutcome.CANCELLED

        # The remote work was in flight, not abandoned: release it now and let the late result
        # fight the revoked authority.
        gate.set()
        await asyncio.wait_for(worker, timeout=5)

        row = invocation_row(world.engine, status=InvocationStatus.AMBIGUOUS)
        assert row["error_code"] == TOOL_OUTCOME_UNKNOWN
        # It had already begun when the owner cancelled, so it is ambiguous, not cancelled.
        assert row["started_at"] is not None
        events = event_rows(world.engine, run_id)
        assert events[-3:] == [
            ("cancellation.requested", None, EXECUTION_CANCELLED),
            ("tool.ambiguous", row["id"], TOOL_OUTCOME_UNKNOWN),
            ("run.cancelled", None, EXECUTION_CANCELLED),
        ]
        types = event_types(world.engine, run_id)
        assert "tool.succeeded" not in types
        assert "tool.failed" not in types
        # No observation and no next model turn: the loop stopped at the fenced call.
        assert len(completion.requests) == 1
        # The Run stays cancelled and is never resurrected by the late result.
        state = run_row(world.engine, run_id)
        assert state["status"] == RunStatus.CANCELLED.value
        assert state["error_code"] is None
        assert job_row(world.engine, run_id)["status"] == "cancelled"
        assert world.server.ledger.counts[WRITE_DOCUMENT] == 1


@pytest.mark.anyio
async def test_journey_o_a_tool_that_outruns_its_deadline_is_ambiguous_through_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-tool deadline, driven through the real service rather than unit-tested at the loop.

    The call reaches the server and is held past `run.limits.tool_timeout_ms`, so the loop's own
    deadline fires. The outcome is ambiguity, never failure: the timeout proves NervOS stopped
    waiting, not that the effect did not land. The Run fails `tool_outcome_unknown`, no observation
    is produced, and nothing is re-dispatched.
    """
    gate = asyncio.Event()
    async with mcp_world(tmp_path, monkeypatch, gate=gate) as world:
        write = world.descriptor(WRITE_DOCUMENT)
        run_id = submit_run(world.engine, instance_id=world.instance_id, limits=TOOL_TIMEOUT_LIMITS)
        claim = claim_only(world.engine, run_id)
        completion = ScriptedCompletion(
            tool_turn(tool_call("call-1", write.model_name, WRITE_ARGUMENTS)),
            final_turn("never reached"),
        )
        execution = build_execution(world.rig, completion=completion)

        outcome = await execution.execute(claim)
        gate.set()

        assert outcome is not None and outcome.status == "failed"
        assert outcome.error_code == TOOL_OUTCOME_UNKNOWN
        row = invocation_row(world.engine, status=InvocationStatus.AMBIGUOUS)
        assert row["error_code"] == TOOL_OUTCOME_UNKNOWN
        # The deadline fires around the dispatch, so `started`->`ambiguous` is the whole transition.
        assert row["started_at"] is not None
        ambiguous = [
            event for event in tool_events(world.engine, run_id) if event[0] == "tool.ambiguous"
        ]
        assert len(ambiguous) == 1
        assert ambiguous[0][1] == row["id"]
        assert ambiguous[0][2] == TOOL_OUTCOME_UNKNOWN
        # No observation was produced and no retry followed.
        assert len(completion.requests) == 1
        assert world.server.ledger.counts[WRITE_DOCUMENT] == 1
        assert job_row(world.engine, run_id)["attempt_count"] == 1
        assert "retry.scheduled" not in event_types(world.engine, run_id)
        state = run_row(world.engine, run_id)
        assert state["status"] == RunStatus.FAILED.value
        assert state["error_code"] == TOOL_OUTCOME_UNKNOWN


@pytest.mark.anyio
async def test_journey_u_the_lease_heartbeat_survives_a_held_call_inside_a_multi_turn_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A healthy Worker keeps renewing its lease while an async tool call is outstanding.

    The heartbeat is independent of the tool await, so a multi-turn loop blocked on a slow remote
    call must not let its own lease lapse. The logical clock is advanced by the test, and the
    renewed lease is read back from `job_attempts`: it moved forward, the Attempt never expired, and
    the real reclamation path finds nothing to reclaim. Existing coverage only ever blocked a single
    model call, never a tool loop.
    """
    gate = asyncio.Event()
    clock = ManualClock()
    async with mcp_world(tmp_path, monkeypatch, gate=gate) as world:
        write = world.descriptor(WRITE_DOCUMENT)
        run_id = submit_run(world.engine, instance_id=world.instance_id)
        claim = claim_only(world.engine, run_id)
        completion = ScriptedCompletion(
            tool_turn(tool_call("call-1", write.model_name, WRITE_ARGUMENTS)),
            final_turn("done"),
        )
        execution = build_execution(
            world.rig,
            completion=completion,
            clock=clock,
            heartbeat_interval=FAST_HEARTBEAT,
            lease_duration=timedelta(minutes=5),
        )

        worker = asyncio.create_task(execution.execute(claim))
        await await_until(
            lambda: (
                invocation_states(world.engine) == [InvocationStatus.STARTED.value]
                and world.server.ledger.counts.get(WRITE_DOCUMENT, 0) == 1
            )
        )
        before = attempt_state(world.engine, claim.attempt_id)

        # Move the logical clock, then wait for a renewal to be written against the later instant.
        clock.advance(timedelta(seconds=10))
        await await_until(
            lambda: (
                attempt_state(world.engine, claim.attempt_id)["last_heartbeat_at"]
                > before["last_heartbeat_at"]
            )
        )
        after = attempt_state(world.engine, claim.attempt_id)

        assert after["status"] == "running"
        assert after["last_heartbeat_at"] > before["last_heartbeat_at"]
        assert after["lease_expires_at"] > before["lease_expires_at"]
        # The independent heartbeat kept the lease current, so the real recovery path finds no
        # expired candidate to reclaim.
        assert reclaim(world.engine, now=clock()) is None

        gate.set()
        outcome = await asyncio.wait_for(worker, timeout=5)

        assert outcome is not None and outcome.status == "succeeded"
        assert outcome.output_text == "done"
        assert attempt_state(world.engine, claim.attempt_id)["status"] == "succeeded"
        assert world.server.ledger.counts[WRITE_DOCUMENT] == 1
        assert invocation_states(world.engine) == [InvocationStatus.SUCCEEDED.value]
        assert run_row(world.engine, run_id)["status"] == RunStatus.SUCCEEDED.value
