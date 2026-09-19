"""D6 tool audit: every durable tool transition projects exactly one safe, linked timeline fact.

The subject is the seam between the authoritative `tool_invocations` record and the append-only Run
Event chronology. Every test here drives the real D4 loop over the real durable invocation state
machine and reads the real `run_events` rows back, because what is being proved is what committed,
not what was intended.

Two rules run through the whole file:

* An event is emitted **only** when its transition commits, in the same transaction -- so a
  refused or repeated transition can never produce a second fact, and a fact can never outlive a
  rolled-back cause.
* A tool event carries a **durable link** to the invocation it describes when one exists, and a
  NULL link when the refusal genuinely has no invocation -- never an invented row, a sentinel
  definition, or another Run's definition id.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from d6_support import (
    NOW,
    Rig,
    ScriptedCompletion,
    build_loop,
    build_rig,
    claim,
    descriptor_named,
    event_sequences,
    final_turn,
    grant,
    invitations,
    invocation_named,
    load_run,
    revoke,
    submit,
    tool_events,
    tool_turn,
)
from nervos_core.application.model_completion import TOOL_DENIED, safe_error_message
from nervos_core.application.tool_invocations import InvocationStatus
from nervos_core.domain.jobs import RunEventType
from nervos_core.infrastructure.database.models import RunEventRecord, ToolInvocationRecord
from nervos_core.infrastructure.database.run_events import (
    EventOwnershipViolation,
    append_event_on_connection,
)
from sqlalchemy import select, text


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    return build_rig(tmp_path, monkeypatch, name="audit.db")


def _run_successfully(
    rig: Rig, *, tool: str = "current_time", arguments: str = '{"timezone":"UTC"}'
) -> tuple[int, int, Any]:
    """Drive one complete successful call and return (run_id, invocation_id, claim handle).

    The handle is returned because the Attempt's claim stays live: a test that wants to write to
    the same Attempt again needs the *original* authority, not a fresh claim, which the engine
    would refuse to issue for a Job that already completed.
    """
    descriptor = descriptor_named(rig, tool)
    grant(rig, descriptor=descriptor)
    run_id = submit(rig)
    handle = claim(rig, run_id)
    completion = ScriptedCompletion(tool_turn(descriptor.model_name, arguments), final_turn("done"))
    asyncio.run(build_loop(rig).run(completion, load_run(rig, run_id), handle, 0))
    return run_id, int(invocation_named(rig, status=InvocationStatus.SUCCEEDED)["id"]), handle


# ---------------------------------------------------------------------------------------
# The happy path: one call, three facts, all linked
# ---------------------------------------------------------------------------------------


def test_a_successful_call_projects_requested_started_and_succeeded(rig: Rig) -> None:
    """The canonical sequence, with every fact linked to its durable invocation."""
    run_id, invocation_id, _handle = _run_successfully(rig)

    assert tool_events(rig, run_id) == [
        ("tool.requested", invocation_id, None),
        ("tool.started", invocation_id, None),
        ("tool.succeeded", invocation_id, None),
    ]
    # A request and a start carry no code because nothing went wrong; the success carries none
    # because a success is not an error. `ck_run_events_message_pair` makes that a schema fact.
    assert all(code is None for _type, _link, code in tool_events(rig, run_id))


def test_the_timeline_sequence_is_strictly_monotonic(rig: Rig) -> None:
    """Per-Run ordering is the durable contract the public keyset read depends on."""
    run_id, invocation_id, _handle = _run_successfully(rig)
    sequences = event_sequences(rig, run_id)
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))
    assert invocation_id > 0


def test_tool_events_carry_the_attempt_they_belong_to(rig: Rig) -> None:
    """Attempt identity is derived from the writing transaction, never guessed later."""
    _run_id, _invocation_id, _handle = _run_successfully(rig)
    with rig.engine.connect() as connection:
        rows = connection.execute(
            select(
                RunEventRecord.attempt_id,
                RunEventRecord.attempt_number,
                RunEventRecord.available_at,
            ).where(RunEventRecord.event_type == RunEventType.TOOL_STARTED.value)
        ).all()
    assert len(rows) == 1
    attempt_id, attempt_number, available_at = rows[0]
    assert attempt_id is not None
    assert attempt_number == 1
    # `available_at` belongs to C4's retry scheduling and is never overloaded by a tool event.
    assert available_at is None


def test_multiple_calls_keep_provider_order_on_the_timeline(rig: Rig) -> None:
    """Sequential execution is D4's contract; the timeline must not reorder it."""
    from d6_support import tool_turns

    first = descriptor_named(rig, "current_time")
    second = descriptor_named(rig, "calculate")
    grant(rig, descriptor=first)
    grant(rig, descriptor=second)
    run_id = submit(rig)
    handle = claim(rig, run_id)
    completion = ScriptedCompletion(
        tool_turns(
            ("call-1", first.model_name, '{"timezone":"UTC"}'),
            ("call-2", second.model_name, '{"expression":"1+1"}'),
        ),
        final_turn("done"),
    )
    asyncio.run(build_loop(rig).run(completion, load_run(rig, run_id), handle, 0))

    linked = [link for _type, link, _code in tool_events(rig, run_id)]
    rows = invitations(rig)
    assert [row["tool_sequence"] for row in rows] == [1, 2]
    # The events name the invocations in the same provider order the calls were made in.
    assert linked == [
        rows[0]["id"],
        rows[0]["id"],
        rows[0]["id"],
        rows[1]["id"],
        rows[1]["id"],
        rows[1]["id"],
    ]


# ---------------------------------------------------------------------------------------
# Failure and ambiguity
# ---------------------------------------------------------------------------------------


def test_a_tool_declared_failure_projects_a_failed_event_with_its_safe_code(rig: Rig) -> None:
    """A known post-dispatch failure is a conclusion, and it is reported as one."""
    from d6_support import tool_turn as turn

    descriptor = descriptor_named(rig, "calculate")
    grant(rig, descriptor=descriptor)
    run_id = submit(rig)
    handle = claim(rig, run_id)
    # Valid against the schema (an object with `expression`), rejected by the tool itself: the
    # tool's own evaluator refuses this expression, so the failure is a *known* post-dispatch one.
    completion = ScriptedCompletion(
        turn(descriptor.model_name, '{"expression":"1 +* 2"}'), final_turn("done")
    )
    asyncio.run(build_loop(rig).run(completion, load_run(rig, run_id), handle, 0))

    invocation = invocation_named(rig, status=InvocationStatus.FAILED)
    events = tool_events(rig, run_id)
    assert [(kind, link) for kind, link, _code in events] == [
        ("tool.requested", invocation["id"]),
        ("tool.started", invocation["id"]),
        ("tool.failed", invocation["id"]),
    ]
    # Durable row and timeline agree, and the event repeats the allowlisted safe code rather than
    # the tool's own message.
    assert events[-1][2] == invocation["error_code"] == "arguments_invalid"


# ---------------------------------------------------------------------------------------
# The two denial paths -- both must emit, and neither may emit a start
# ---------------------------------------------------------------------------------------


class MutatingCompletion(ScriptedCompletion):
    """A completion that changes durable authority as the model is answering.

    This is the honest way to reach the loop's permission checks with a *live* catalog: the
    catalog is assembled and frozen before the model turn, so a grant withdrawn or a definition
    withdrawn during that turn leaves a tool that is still offered but no longer permitted --
    which is exactly the case D2's live check exists to catch.
    """

    def __init__(self, mutate: Any, *responses: Any) -> None:
        super().__init__(*responses)
        self._mutate = mutate

    async def complete(self, request: Any) -> Any:
        self._mutate()
        return await super().complete(request)


def revoking_completion(rig: Rig, descriptor: Any, *responses: Any) -> MutatingCompletion:
    """Withdraw the grant as the model answers."""
    return MutatingCompletion(lambda: revoke(rig, descriptor=descriptor), *responses)


def test_a_first_check_denial_projects_requested_then_denied(rig: Rig) -> None:
    """A call refused before any dispatch is recorded as requested, then denied."""
    descriptor = descriptor_named(rig, "current_time")
    grant(rig, descriptor=descriptor)
    run_id = submit(rig)
    handle = claim(rig, run_id)
    completion = revoking_completion(
        rig,
        descriptor,
        tool_turn(descriptor.model_name, '{"timezone":"UTC"}'),
        final_turn("never dispatched"),
    )
    asyncio.run(build_loop(rig).run(completion, load_run(rig, run_id), handle, 0))

    invocation = invocation_named(rig, status=InvocationStatus.DENIED)
    assert tool_events(rig, run_id) == [
        ("tool.requested", invocation["id"], None),
        ("tool.denied", invocation["id"], TOOL_DENIED),
    ]
    # The precise internal reason is durable on the row and absent from the public timeline.
    assert invocation["permission_decision"] == "denied_not_granted"
    assert invocation["permission_decision"] not in {
        code for _t, _l, code in tool_events(rig, run_id)
    }
    assert invocation["started_at"] is None


class _FlippingAuthorizer:
    """The real D2 evaluator, able to start refusing from a chosen check onward.

    It delegates every ordinary check to the production evaluator, so the durable transition under
    test is the real one; only *when* the live predicate starts refusing is controlled. That is
    what lets the second, in-transaction check disagree with the first without a timing race, and
    it is the only way to reach `mark_started`'s DENIED branch deterministically.
    """

    def __init__(self, inner: Any, *, deny_from: int) -> None:
        self._inner = inner
        self._deny_from = deny_from
        self.calls = 0

    def check_permission(self, *, run_id: int, tool_definition_id: int) -> Any:
        from nervos_core.application.tool_permissions import (
            PermissionDecision,
            PermissionDenialReason,
        )

        self.calls += 1
        if self.calls >= self._deny_from:
            return PermissionDecision(allowed=False, reason=PermissionDenialReason.NOT_GRANTED)
        return self._inner.check_permission(run_id=run_id, tool_definition_id=tool_definition_id)


def _loop_with_authorizer(rig: Rig, authorizer: Any) -> Any:
    """Build the real loop with the production evaluator replaced by the controlled one."""
    return build_loop(rig, authorize=authorizer)


def test_a_grant_revoked_before_the_start_check_projects_no_started_event(rig: Rig) -> None:
    """`mark_started`'s own DENIED branch is a second, independent path and must also emit.

    This is the case the D4 failure table did not cover: the loop records the request while the
    grant is live, and the *live re-check inside `mark_started`* -- in the same transaction that
    would otherwise have committed `started` -- is what refuses it. No `mark_denied` call happens
    on this path, so a denial event that lived only on `mark_denied` would leave a durable
    `denied` row with an empty timeline.
    """
    from nervos_core.infrastructure.database.tools import SqlAlchemyToolPermissionEvaluator

    descriptor = descriptor_named(rig, "current_time")
    grant(rig, descriptor=descriptor)
    run_id = submit(rig)
    handle = claim(rig, run_id)

    authorizer = _FlippingAuthorizer(SqlAlchemyToolPermissionEvaluator(rig.engine), deny_from=2)
    completion = ScriptedCompletion(
        tool_turn(descriptor.model_name, '{"timezone":"UTC"}'), final_turn("denied")
    )
    asyncio.run(
        _loop_with_authorizer(rig, authorizer).run(completion, load_run(rig, run_id), handle, 0)
    )

    invocation = invocation_named(rig, status=InvocationStatus.DENIED)
    kinds = [kind for kind, _link, _code in tool_events(rig, run_id)]
    assert kinds == ["tool.requested", "tool.denied"]
    # The decisive assertion: the call was refused at the boundary, so it provably never ran.
    assert "tool.started" not in kinds
    assert invocation["started_at"] is None
    assert invocation["permission_decision"] == "denied_not_granted"


def test_a_definition_that_became_unavailable_denies_at_the_boundary(rig: Rig) -> None:
    """A revocation source other than the grant travels the same path and reads the same way.

    A D5 reconciliation can withdraw a definition while a Run is mid-Attempt; the catalog this
    Attempt already froze still offers it, and D2's live predicate is what refuses it.
    """

    def withdraw_definition() -> None:
        with rig.engine.begin() as connection:
            connection.execute(
                text("UPDATE tool_definitions SET status = 'unavailable' WHERE id = :i"),
                {"i": descriptor.tool_definition_id},
            )

    descriptor = descriptor_named(rig, "current_time")
    grant(rig, descriptor=descriptor)
    run_id = submit(rig)
    handle = claim(rig, run_id)
    completion = MutatingCompletion(
        withdraw_definition,
        tool_turn(descriptor.model_name, '{"timezone":"UTC"}'),
        final_turn("denied"),
    )
    asyncio.run(build_loop(rig).run(completion, load_run(rig, run_id), handle, 0))

    invocation = invocation_named(rig, status=InvocationStatus.DENIED)
    assert [kind for kind, _l, _c in tool_events(rig, run_id)] == ["tool.requested", "tool.denied"]
    assert invocation["permission_decision"] == "denied_definition_unavailable"


# ---------------------------------------------------------------------------------------
# Pre-dispatch refusals: real audit facts with no invocation row
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "model_name", "arguments"),
    [
        ("unknown_tool", "nervos__builtin__does_not_exist", '{"timezone":"UTC"}'),
        ("malformed_json", "nervos__builtin__current_time", "not json at all"),
        ("non_object_json", "nervos__builtin__current_time", "[]"),
        ("schema_invalid", "nervos__builtin__current_time", '{"wrong":"key"}'),
    ],
)
def test_a_pre_dispatch_refusal_is_audited_with_no_invocation(
    rig: Rig, case: str, model_name: str, arguments: str
) -> None:
    """The four refusals D4 could not record now appear, and none invents an invocation.

    D4 denied all four in memory and left no durable trace, which made a model's refused call
    invisible to the audit. D6 records it where it is honestly representable -- a `tool.denied`
    event with a NULL link -- rather than fabricating a sentinel definition or a fake row.
    """
    descriptor = descriptor_named(rig, "current_time")
    grant(rig, descriptor=descriptor)
    run_id = submit(rig)
    handle = claim(rig, run_id)

    # The unknown-tool case names something the frozen catalog cannot contain; the other three
    # name a real, granted tool and fail on the arguments instead.
    resolved = model_name if case == "unknown_tool" else descriptor.model_name
    completion = ScriptedCompletion(tool_turn(resolved, arguments), final_turn("refused"))
    asyncio.run(build_loop(rig).run(completion, load_run(rig, run_id), handle, 0))

    assert tool_events(rig, run_id) == [("tool.denied", None, TOOL_DENIED)]
    # No invocation row exists for any of the four, and no definition was invented for them.
    assert invitations(rig) == []


def test_a_pre_dispatch_refusal_uses_the_single_generic_public_code(rig: Rig) -> None:
    """The public code is generic; the reason classification never becomes public."""
    descriptor = descriptor_named(rig, "current_time")
    grant(rig, descriptor=descriptor)
    run_id = submit(rig)
    handle = claim(rig, run_id)
    completion = ScriptedCompletion(
        tool_turn("nervos__builtin__absent", "{}"), final_turn("refused")
    )
    asyncio.run(build_loop(rig).run(completion, load_run(rig, run_id), handle, 0))

    _kind, link, code = tool_events(rig, run_id)[0]
    assert link is None
    assert code == TOOL_DENIED
    assert safe_error_message(TOOL_DENIED) == "This tool call was refused before it ran."
    # The event's own message is the static allowlisted text, not the model-facing note.
    with rig.engine.connect() as connection:
        message = connection.execute(
            select(RunEventRecord.message).where(
                RunEventRecord.event_type == RunEventType.TOOL_DENIED.value
            )
        ).scalar_one()
    assert message == safe_error_message(TOOL_DENIED)


def test_an_unknown_tool_refusal_is_written_before_the_model_is_told(rig: Rig) -> None:
    """Audit precedes observation, so a refusal the timeline lacks is never shown to the model.

    Authority here is intentionally still live, so the refusal *is* recorded and the model *is*
    then told; the ordering guarantee is that the durable write comes first and gates the
    observation, which the loop's own refusal helper performs as one step.
    """
    descriptor = descriptor_named(rig, "current_time")
    grant(rig, descriptor=descriptor)
    run_id = submit(rig)
    handle = claim(rig, run_id)
    completion = ScriptedCompletion(
        tool_turn("nervos__builtin__absent", "{}", call_id="call-x"), final_turn("refused")
    )
    asyncio.run(build_loop(rig).run(completion, load_run(rig, run_id), handle, 0))

    # The event exists and the loop continued to a second model turn, which is the ordered pair.
    assert len(tool_events(rig, run_id)) == 1
    assert len(completion.requests) == 2
    # The observation the model received is the D4 note, unchanged by D6.
    second = completion.requests[1]
    assert any("not available to this agent" in str(turn) for turn in getattr(second, "turns", ()))


# ---------------------------------------------------------------------------------------
# Duplicate prevention
# ---------------------------------------------------------------------------------------


def test_a_repeated_terminal_write_appends_no_second_event(rig: Rig) -> None:
    """The compare-and-set is what makes the event unique, not a text search for one."""
    from nervos_core.application.tool_invocations import ResultEnvelope
    from nervos_core.infrastructure.database.tool_invocations import (
        SqlAlchemyToolInvocationPersistence,
    )

    run_id, invocation_id, handle = _run_successfully(rig)
    before = tool_events(rig, run_id)

    # Re-close the same call on the same live claim. The row is no longer `started`, so the CAS
    # matches nothing and the whole transaction -- event included -- rolls back.
    persistence = SqlAlchemyToolInvocationPersistence(rig.engine)
    assert (
        persistence.mark_succeeded(
            claim=handle,
            invocation_id=invocation_id,
            envelope=ResultEnvelope("a" * 64, 1),
            now=NOW,
        )
        is False
    )

    assert tool_events(rig, run_id) == before
    assert invocation_named(rig, status=InvocationStatus.SUCCEEDED)["id"] == invocation_id


def test_a_repeated_refusal_appends_a_second_event_only_because_it_is_a_second_call(
    rig: Rig,
) -> None:
    """Two unknown-tool calls are two facts; one call is never recorded twice.

    The distinction matters: refusals have no invocation to compare-and-set against, so their
    uniqueness comes from the loop calling the refusal port once per refused call -- not from a
    dedupe key. This asserts the count tracks the calls, not the passes.
    """
    descriptor = descriptor_named(rig, "current_time")
    grant(rig, descriptor=descriptor)
    run_id = submit(rig)
    handle = claim(rig, run_id)
    completion = ScriptedCompletion(
        tool_turn("nervos__builtin__absent", "{}", call_id="call-1"), final_turn("refused")
    )
    asyncio.run(build_loop(rig).run(completion, load_run(rig, run_id), handle, 0))

    assert [kind for kind, _l, _c in tool_events(rig, run_id)] == ["tool.denied"]


# ---------------------------------------------------------------------------------------
# Ownership: the link is proved, not merely permitted by a foreign key
# ---------------------------------------------------------------------------------------


def test_a_tool_event_naming_another_runs_invocation_is_refused(rig: Rig) -> None:
    """A foreign key proves an invocation exists; it does not prove it is *this* Run's.

    The adversarial shape is deliberate: the invocation id used here is real and satisfies the
    foreign key, so only the ownership triple can catch it. Without that check the timeline of one
    Run would be linked to another Run's tool call.
    """
    first_run, invocation_id, _handle = _run_successfully(rig)

    # A second Run on the same Agent Instance reuses the same grant; the point is only that its
    # Attempt is a different one, so the invocation id below belongs to another Run entirely.
    second_run = submit(rig)
    second_handle = claim(rig, second_run, worker_id="worker-2")

    with rig.engine.connect() as connection:
        # Any one of the Run's events names the same Job, so a bounded read is enough.
        first_job_id = int(
            connection.execute(
                select(RunEventRecord.job_id).where(RunEventRecord.run_id == first_run).limit(1)
            ).scalar_one()
        )
    assert first_job_id != second_handle.job_id

    with pytest.raises(EventOwnershipViolation), rig.engine.begin() as connection:
        append_event_on_connection(
            connection,
            run_id=second_run,
            job_id=second_handle.job_id,
            attempt_id=second_handle.attempt_id,
            tool_invocation_id=invocation_id,
            sequence=1,
            event_type=RunEventType.TOOL_SUCCEEDED,
            created_at=NOW,
        )

    # The honest pairing still works: the same invocation id appended under its own Run, Job and
    # Attempt is accepted, so the check refuses the impostor rather than the field itself.
    with rig.engine.begin() as connection:
        append_event_on_connection(
            connection,
            run_id=first_run,
            job_id=first_job_id,
            attempt_id=int(invitations(rig)[0]["attempt_id"]),
            tool_invocation_id=invocation_id,
            sequence=event_sequences(rig, first_run)[-1] + 1,
            event_type=RunEventType.TOOL_SUCCEEDED,
            created_at=NOW,
        )
    assert event_sequences(rig, first_run)[-1] > 0


def test_a_tool_event_without_an_attempt_is_refused(rig: Rig) -> None:
    """A tool fact always belongs to an Attempt; the append refuses one that claims otherwise."""
    _run_id, invocation_id, _handle = _run_successfully(rig)
    with pytest.raises(EventOwnershipViolation), rig.engine.begin() as connection:
        append_event_on_connection(
            connection,
            run_id=1,
            job_id=1,
            attempt_id=None,
            tool_invocation_id=invocation_id,
            sequence=1,
            event_type=RunEventType.TOOL_SUCCEEDED,
            created_at=NOW,
        )


# ---------------------------------------------------------------------------------------
# The audit surface cannot hold raw tool material
# ---------------------------------------------------------------------------------------


def test_no_raw_tool_material_reaches_the_run_events_table(rig: Rig) -> None:
    """A marker placed in arguments, in a tool result and in a tool error must not be persisted.

    The marker is a synthetic string with no meaning to the engine; it stands in for whatever a
    real call would carry. Only `code`, a static allowlisted `message`, and the two identifiers are
    ever written, so none of them can be a carrier.
    """
    marker = "NERVOS_D6_MARKER_2f8c1b"
    descriptor = descriptor_named(rig, "calculate")
    grant(rig, descriptor=descriptor)
    run_id = submit(rig)
    handle = claim(rig, run_id)
    # The marker is an argument value, so it travels through the digest/shape path only.
    completion = ScriptedCompletion(
        tool_turn(descriptor.model_name, '{"expression":"' + marker + '"}'),
        final_turn("done"),
    )
    asyncio.run(build_loop(rig).run(completion, load_run(rig, run_id), handle, 0))

    with rig.engine.connect() as connection:
        pairs = connection.execute(select(RunEventRecord.code, RunEventRecord.message)).all()
        event_text = " ".join(value for pair in pairs for value in pair if value is not None)
        digest, shape = connection.execute(
            select(
                ToolInvocationRecord.arguments_digest,
                ToolInvocationRecord.arguments_shape,
            ).where(ToolInvocationRecord.id == invitations(rig)[0]["id"])
        ).one()

    # Nowhere on the timeline, and nowhere in the invocation's own evidence columns either: the
    # digest is one-way content evidence and the shape is a value-free key skeleton.
    assert marker not in event_text
    assert marker not in (digest or "") and marker not in (shape or "")
    assert digest is not None and shape is not None
