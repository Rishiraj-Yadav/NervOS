"""D6 cancellation semantics: the owner's cancellation never leaves a tool fact behind.

This suite is the cancellation half of D6's subject. The audit suite proves that every durable tool
transition projects exactly one linked timeline fact; this one proves the complementary property --
that when the *owner* cancels, the engine reconciles whatever the Worker left dangling so that the
durable record is never ambiguous about a call:

* A call still in ``requested`` provably never dispatched, so it is closed ``cancelled`` and emits
  **no** tool event. The six frozen tool event types deliberately contain no ``tool.cancelled``,
  so a reader learns the call's fate from its own row and the Run's cancellation events, never
  from an invented per-tool fact.
* A call already ``started`` may have reached an external system, so it is closed ``ambiguous`` with
  ``tool_outcome_unknown`` and gets one ``tool.ambiguous`` event, ordered between
  ``cancellation.requested`` and ``run.cancelled``.

Two rules run through the whole file:

* **Cancellation is authoritative.** The Run ends ``cancelled`` with no error pair, the Job ends
  ``cancelled`` with ``execution_cancelled``, ``cancel_requested_at`` is write-once, and a repeat
  appends nothing.
* **A late result can never resurrect the Run.** Once cancellation has committed, the original
  claim's terminal writes are all fenced, so no call can be concluded as succeeded or failed after
  the Run was cancelled, and no post-cancellation event is ever appended.

Every test drives the real C5 seam over the real durable invocation state machine and reads the real
``run_events``, ``tool_invocations``, ``runs``, and ``jobs`` rows back, because the subject is what
*committed*, not what was intended.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from d6_support import (
    LATER,
    LEASE,
    NOW,
    PermissionDecision,
    Rig,
    ScriptedCompletion,
    build_loop,
    build_rig,
    cancel,
    claim,
    descriptor_named,
    event_sequences,
    event_types,
    events,
    final_turn,
    grant,
    invocation_named,
    load_run,
    run_row,
    submit,
    tool_events,
    tool_turn,
)
from nervos_core.application.job_execution import CancellationOutcome
from nervos_core.application.model_completion import (
    EXECUTION_CANCELLED,
    INTERNAL_EXECUTION_ERROR,
    TOOL_OUTCOME_UNKNOWN,
    ModelProviderError,
    safe_error_message,
)
from nervos_core.application.tool_invocations import (
    InvocationRequest,
    InvocationStatus,
    RecordOutcomeKind,
    StartOutcomeKind,
    result_envelope,
)
from nervos_core.domain.jobs import AttemptStatus, JobStatus
from nervos_core.domain.runs import RunStatus
from nervos_core.infrastructure.database.jobs import SqlAlchemyJobExecutionPersistence
from nervos_core.infrastructure.database.tool_invocations import (
    SqlAlchemyToolInvocationPersistence,
)
from sqlalchemy import text

TOOL = "current_time"
ARGUMENTS = '{"timezone":"UTC"}'


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    return build_rig(tmp_path, monkeypatch, name="cancel-semantics.db")


class MutatingCompletion(ScriptedCompletion):
    """A completion that changes durable authority as the model is answering.

    Cancellation commits on a *different* connection than the loop's, so a barrier that runs inside
    ``complete`` is the honest way to reach the exact instant the suite cares about: the model turn
    is in flight, no tool call has been dispatched by *this* Worker, and the Run is already durably
    cancelled.
    """

    def __init__(self, mutate: Any, *responses: Any) -> None:
        super().__init__(*responses)
        self._mutate = mutate

    async def complete(self, request: Any) -> Any:
        self._mutate()
        return await super().complete(request)


def _claim_without_start(rig: Rig, run_id: int) -> Any:
    """Claim the Run's Attempt but never cross the execution start boundary."""
    persistence = SqlAlchemyJobExecutionPersistence(rig.engine)
    handle = persistence.claim_next(
        worker_id="worker-1",
        provider_ids=("anthropic",),
        max_active=4,
        now=NOW,
        lease_duration=LEASE,
    )
    assert handle is not None
    assert handle.run_id == run_id
    return handle


def _invocation_request(
    descriptor: Any, handle: Any, *, call_id: str = "call-1"
) -> InvocationRequest:
    """Build one request bound to a real claim, so the row's foreign keys are genuine."""
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
        provider_call_id=call_id,
        permission_decision=PermissionDecision(allowed=True),
        arguments={"timezone": "UTC"},
    )


def _record_requested(rig: Rig, descriptor: Any, handle: Any) -> tuple[Any, int]:
    """Record one ``requested`` row through the real production path, returning its id."""
    persistence = SqlAlchemyToolInvocationPersistence(rig.engine)
    outcome = persistence.record_requested(
        claim=handle, request=_invocation_request(descriptor, handle), now=NOW
    )
    assert outcome.kind is RecordOutcomeKind.REQUESTED
    assert outcome.invocation_id is not None
    return persistence, int(outcome.invocation_id)


def _record_started(rig: Rig, descriptor: Any, handle: Any) -> tuple[Any, int]:
    """Record a ``requested`` row and cross the start boundary, as a dispatch would."""
    persistence, invocation_id = _record_requested(rig, descriptor, handle)
    started = persistence.mark_started(claim=handle, invocation_id=invocation_id, now=NOW)
    assert started.kind is StartOutcomeKind.STARTED
    return persistence, invocation_id


def _attempt_row(rig: Rig, attempt_id: int) -> dict[str, Any]:
    with rig.engine.connect() as connection:
        return dict(
            connection.execute(
                text("SELECT status, execution_started_at FROM job_attempts WHERE id = :a"),
                {"a": attempt_id},
            )
            .mappings()
            .one()
        )


def _job_row(rig: Rig, run_id: int) -> dict[str, Any]:
    with rig.engine.connect() as connection:
        return dict(
            connection.execute(
                text("SELECT status, error_code, cancel_requested_at FROM jobs WHERE run_id = :r"),
                {"r": run_id},
            )
            .mappings()
            .one()
        )


# ---------------------------------------------------------------------------------------
# Pre-tool cancellation: nothing was ever requested, so the timeline is untouched
# ---------------------------------------------------------------------------------------


def test_cancelling_a_queued_run_with_no_attempt_adds_no_tool_event(rig: Rig) -> None:
    """A Run that never began has exactly the frozen two-event cancellation timeline."""
    run_id = submit(rig)

    outcome = cancel(rig, run_id, now=LATER)

    assert outcome is CancellationOutcome.CANCELLED
    assert event_types(rig, run_id) == [
        "run.created",
        "run.queued",
        "cancellation.requested",
        "run.cancelled",
    ]
    assert tool_events(rig, run_id) == []
    assert run_row(rig, run_id)["status"] == RunStatus.CANCELLED.value


def test_cancelling_a_claimed_never_started_attempt_adds_no_tool_event(rig: Rig) -> None:
    """A pre-start claim has no start boundary, so no invocation can exist to reconcile."""
    run_id = submit(rig)
    handle = _claim_without_start(rig, run_id)
    assert _attempt_row(rig, handle.attempt_id)["execution_started_at"] is None

    outcome = cancel(rig, run_id, now=LATER)

    assert outcome is CancellationOutcome.CANCELLED
    assert event_types(rig, run_id)[-2:] == ["cancellation.requested", "run.cancelled"]
    assert tool_events(rig, run_id) == []
    assert _attempt_row(rig, handle.attempt_id)["status"] == AttemptStatus.CANCELLED.value
    assert run_row(rig, run_id)["status"] == RunStatus.CANCELLED.value


def test_cancelling_while_the_model_turn_is_in_flight_adds_no_tool_event(rig: Rig) -> None:
    """The revocation lands mid-turn: the loop's next write is fenced and no call runs."""
    descriptor = descriptor_named(rig, TOOL)
    grant(rig, descriptor=descriptor)
    run_id = submit(rig)
    handle = claim(rig, run_id)
    completion = MutatingCompletion(
        lambda: cancel(rig, run_id, now=LATER),
        tool_turn(descriptor.model_name, ARGUMENTS),
    )

    # The model answered a cancelled Run: the loop cannot persist usage or dispatch, and stops.
    with pytest.raises(ModelProviderError):
        asyncio.run(build_loop(rig).run(completion, load_run(rig, run_id), handle, 0))

    assert tool_events(rig, run_id) == []
    assert event_types(rig, run_id)[-2:] == ["cancellation.requested", "run.cancelled"]
    assert run_row(rig, run_id)["status"] == RunStatus.CANCELLED.value


# ---------------------------------------------------------------------------------------
# Post-request cancellation: a row that provably never dispatched
# ---------------------------------------------------------------------------------------


def test_cancelling_before_dispatch_closes_the_requested_row_as_cancelled(rig: Rig) -> None:
    """A ``requested`` row is closed ``cancelled`` with a NULL start and emits no tool event."""
    descriptor = descriptor_named(rig, TOOL)
    grant(rig, descriptor=descriptor)
    run_id = submit(rig)
    handle = claim(rig, run_id)
    _persistence, invocation_id = _record_requested(rig, descriptor, handle)

    outcome = cancel(rig, run_id, now=LATER)

    assert outcome is CancellationOutcome.CANCELLED
    row = invocation_named(rig, status=InvocationStatus.CANCELLED)
    assert row["id"] == invocation_id
    # The CHECK makes this a schema fact: a cancelled call can never claim it started.
    assert row["started_at"] is None
    assert row["finished_at"] is not None
    types = event_types(rig, run_id)
    assert types[-2:] == ["cancellation.requested", "run.cancelled"]
    assert "tool.ambiguous" not in types
    # The only tool fact is the request the Worker recorded before it was revoked.
    assert tool_events(rig, run_id) == [("tool.requested", invocation_id, None)]


# ---------------------------------------------------------------------------------------
# Post-dispatch cancellation: a row that may already have reached an external system
# ---------------------------------------------------------------------------------------


def test_cancelling_a_dispatched_call_closes_it_ambiguous_between_the_two_run_events(
    rig: Rig,
) -> None:
    """The one ordered tail: request, then the ambiguous call, then the terminal Run event."""
    descriptor = descriptor_named(rig, TOOL)
    grant(rig, descriptor=descriptor)
    run_id = submit(rig)
    handle = claim(rig, run_id)
    _persistence, invocation_id = _record_started(rig, descriptor, handle)

    outcome = cancel(rig, run_id, now=LATER)

    assert outcome is CancellationOutcome.CANCELLED
    row = invocation_named(rig, status=InvocationStatus.AMBIGUOUS)
    assert row["id"] == invocation_id
    assert row["error_code"] == TOOL_OUTCOME_UNKNOWN
    assert row["error_message"] == safe_error_message(TOOL_OUTCOME_UNKNOWN)
    assert row["started_at"] is not None

    assert events(rig, run_id)[-3:] == [
        ("cancellation.requested", None, EXECUTION_CANCELLED),
        ("tool.ambiguous", invocation_id, TOOL_OUTCOME_UNKNOWN),
        ("run.cancelled", None, EXECUTION_CANCELLED),
    ]
    sequences = event_sequences(rig, run_id)[-3:]
    assert sequences[0] < sequences[1] < sequences[2]
    types = event_types(rig, run_id)
    assert types.index("cancellation.requested") < types.index("tool.ambiguous")
    assert types.index("tool.ambiguous") < types.index("run.cancelled")


def test_repeated_cancellation_is_idempotent(rig: Rig) -> None:
    """A second owner request appends nothing and never moves the write-once instant."""
    descriptor = descriptor_named(rig, TOOL)
    grant(rig, descriptor=descriptor)
    run_id = submit(rig)
    handle = claim(rig, run_id)
    _persistence, _invocation_id = _record_started(rig, descriptor, handle)

    assert cancel(rig, run_id, now=NOW) is CancellationOutcome.CANCELLED
    first_stamp = _job_row(rig, run_id)["cancel_requested_at"]

    assert cancel(rig, run_id, now=LATER) is CancellationOutcome.CANCELLED

    assert _job_row(rig, run_id)["cancel_requested_at"] == first_stamp
    types = event_types(rig, run_id)
    assert types.count("cancellation.requested") == 1
    assert types.count("run.cancelled") == 1
    assert types.count("tool.ambiguous") == 1


def test_a_late_result_is_fenced_and_cannot_resurrect_the_run(rig: Rig) -> None:
    """Every terminal write the original claim attempts after cancellation is refused."""
    descriptor = descriptor_named(rig, TOOL)
    grant(rig, descriptor=descriptor)
    run_id = submit(rig)
    handle = claim(rig, run_id)
    _persistence, invocation_id = _record_started(rig, descriptor, handle)
    assert cancel(rig, run_id, now=LATER) is CancellationOutcome.CANCELLED

    late = SqlAlchemyToolInvocationPersistence(rig.engine)
    assert (
        late.mark_succeeded(
            claim=handle,
            invocation_id=invocation_id,
            envelope=result_envelope(text="late success", structured=None),
            now=NOW,
        )
        is False
    )
    assert (
        late.mark_failed(
            claim=handle,
            invocation_id=invocation_id,
            error_code=INTERNAL_EXECUTION_ERROR,
            error_message=safe_error_message(INTERNAL_EXECUTION_ERROR),
            now=NOW,
        )
        is False
    )
    assert (
        late.mark_ambiguous(
            claim=handle,
            invocation_id=invocation_id,
            error_code=TOOL_OUTCOME_UNKNOWN,
            error_message=safe_error_message(TOOL_OUTCOME_UNKNOWN),
            now=NOW,
        )
        is False
    )

    # The reconciled fact stands untouched, the Run stays cancelled, and no late event exists.
    row = invocation_named(rig, status=InvocationStatus.AMBIGUOUS)
    assert row["id"] == invocation_id
    assert row["error_code"] == TOOL_OUTCOME_UNKNOWN
    assert run_row(rig, run_id)["status"] == RunStatus.CANCELLED.value
    types = event_types(rig, run_id)
    assert "tool.succeeded" not in types
    assert "tool.failed" not in types
    assert types.count("tool.ambiguous") == 1
    assert _job_row(rig, run_id)["error_code"] == EXECUTION_CANCELLED


# ---------------------------------------------------------------------------------------
# Cancellation after completed work: the success is never reinterpreted
# ---------------------------------------------------------------------------------------


def test_cancelling_after_a_successful_call_keeps_the_success_and_adds_no_ambiguity(
    rig: Rig,
) -> None:
    """A concluded call is evidence, so reconciliation leaves it alone and emits no ambiguity."""
    descriptor = descriptor_named(rig, TOOL)
    grant(rig, descriptor=descriptor)
    run_id = submit(rig)
    handle = claim(rig, run_id)

    turns = {"count": 0}

    def cancel_on_second_turn() -> None:
        turns["count"] += 1
        if turns["count"] == 2:
            cancel(rig, run_id, now=LATER)

    completion = MutatingCompletion(
        cancel_on_second_turn,
        tool_turn(descriptor.model_name, ARGUMENTS),
        final_turn("done"),
    )

    # The first turn's call succeeds; the second turn is answered for a cancelled Run and stops.
    with pytest.raises(ModelProviderError):
        asyncio.run(build_loop(rig).run(completion, load_run(rig, run_id), handle, 0))

    succeeded = invocation_named(rig, status=InvocationStatus.SUCCEEDED)
    assert succeeded["error_code"] is None
    assert succeeded["finished_at"] is not None
    types = event_types(rig, run_id)
    # The completion is left as the fact it was, and only the Run-level cancellation is added.
    assert types[-3:] == ["tool.succeeded", "cancellation.requested", "run.cancelled"]
    assert "tool.ambiguous" not in types
    assert run_row(rig, run_id)["status"] == RunStatus.CANCELLED.value
    assert _job_row(rig, run_id)["status"] == JobStatus.CANCELLED.value
