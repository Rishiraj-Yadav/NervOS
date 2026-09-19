"""D6 integrated the tool layer with C6 and did not add a second execution primitive.

The claim under test is narrow and load-bearing: a multi-turn Think -> Act -> Observe loop is
*one* durable obligation. No job per model turn, no job per tool call, no extra Attempt to carry
the loop forward, and no tool-specific queue at all -- the Run's Single Job holds its concurrency
slot from the first model call to the last observation, exactly as ADR 0017 requires.

Everything here runs against the real submission, claim and loop paths, because the thing being
counted is what the engine durably created.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from d6_support import (
    NOW,
    Rig,
    ScriptedCompletion,
    build_loop,
    build_rig,
    claim,
    descriptor_named,
    final_turn,
    grant,
    invitations,
    load_run,
    submit,
    tool_turn,
    tool_turns,
)
from nervos_core.application.tool_invocations import InvocationStatus
from nervos_core.domain.runs import TOOL_ENABLED_LIMITS, RunLimits
from nervos_core.infrastructure.database.models import (
    JobAttemptRecord,
    JobRecord,
    RunRecord,
)
from sqlalchemy import func, select


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    return build_rig(tmp_path, monkeypatch, name="c6.db")


def _counts(rig: Rig, run_id: int) -> tuple[int, int, int]:
    """Return (runs, jobs, attempts) for one Run."""
    with rig.engine.connect() as connection:
        runs = int(
            connection.execute(
                select(func.count()).select_from(RunRecord).where(RunRecord.id == run_id)
            ).scalar_one()
        )
        jobs = int(
            connection.execute(
                select(func.count()).select_from(JobRecord).where(JobRecord.run_id == run_id)
            ).scalar_one()
        )
        attempts = int(
            connection.execute(
                select(func.count())
                .select_from(JobAttemptRecord)
                .join(JobRecord, JobRecord.id == JobAttemptRecord.job_id)
                .where(JobRecord.run_id == run_id)
            ).scalar_one()
        )
    return runs, jobs, attempts


def test_a_multi_turn_tool_loop_is_still_one_run_one_job_and_one_attempt(rig: Rig) -> None:
    """Three model turns and two tool calls add no durable obligation of their own.

    This is the assertion that would fail first if the loop were ever re-implemented as a job per
    turn or a continuation job: the counts are independent of how many times the model was
    consulted and how many tools it asked for.
    """
    first = descriptor_named(rig, "current_time")
    second = descriptor_named(rig, "calculate")
    grant(rig, descriptor=first)
    grant(rig, descriptor=second)
    run_id = submit(rig)
    handle = claim(rig, run_id)

    completion = ScriptedCompletion(
        tool_turn(first.model_name, '{"timezone":"UTC"}', call_id="call-1"),
        tool_turn(second.model_name, '{"expression":"1+1"}', call_id="call-2"),
        final_turn("both done"),
    )
    asyncio.run(build_loop(rig).run(completion, load_run(rig, run_id), handle, 0))

    assert len(completion.requests) == 3
    assert len(invitations(rig)) == 2
    assert [row["status"] for row in invitations(rig)] == [
        InvocationStatus.SUCCEEDED.value,
        InvocationStatus.SUCCEEDED.value,
    ]
    # One Run, one Job, one Attempt -- regardless of the turns and the calls above.
    assert _counts(rig, run_id) == (1, 1, 1)


def test_several_calls_in_one_model_turn_stay_sequential_in_one_attempt(rig: Rig) -> None:
    """A batch of calls is executed in provider order inside the Attempt that already exists."""
    first = descriptor_named(rig, "current_time")
    second = descriptor_named(rig, "calculate")
    grant(rig, descriptor=first)
    grant(rig, descriptor=second)
    run_id = submit(rig)
    handle = claim(rig, run_id)

    completion = ScriptedCompletion(
        tool_turns(
            ("call-a", first.model_name, '{"timezone":"UTC"}'),
            ("call-b", second.model_name, '{"expression":"2*3"}'),
        ),
        final_turn("both done"),
    )
    asyncio.run(build_loop(rig).run(completion, load_run(rig, run_id), handle, 0))

    rows = invitations(rig)
    assert [row["tool_sequence"] for row in rows] == [1, 2]
    assert _counts(rig, run_id) == (1, 1, 1)


def test_a_long_tool_sequence_does_not_multiply_durable_obligations(rig: Rig) -> None:
    """Up to the Run's own tool budget, the durable shape is unchanged.

    The budget is the Run's, snapshotted at submission; reaching it is a loop outcome, not a
    reason for the engine to create anything new.
    """
    descriptor = descriptor_named(rig, "current_time")
    grant(rig, descriptor=descriptor)
    limits = RunLimits(max_model_calls=8, max_tool_calls=4)
    run_id = submit(rig, limits=limits)
    handle = claim(rig, run_id)

    completion = ScriptedCompletion(
        *[
            tool_turn(descriptor.model_name, '{"timezone":"UTC"}', call_id=f"call-{index}")
            for index in range(4)
        ],
        final_turn("done"),
    )
    asyncio.run(build_loop(rig).run(completion, load_run(rig, run_id), handle, 0))

    assert len(invitations(rig)) == 4
    assert _counts(rig, run_id) == (1, 1, 1)


def test_the_attempt_keeps_its_claim_across_the_whole_loop(rig: Rig) -> None:
    """The claim taken for the first model call is still the one the last tool call uses.

    A loop that released and re-acquired concurrency between turns would show up here as a claim
    that no longer matched, and it would also mean a Run could lose its slot mid-thought.
    """
    descriptor = descriptor_named(rig, "current_time")
    grant(rig, descriptor=descriptor)
    run_id = submit(rig, limits=TOOL_ENABLED_LIMITS)
    handle = claim(rig, run_id)

    completion = ScriptedCompletion(
        tool_turn(descriptor.model_name, '{"timezone":"UTC"}', call_id="call-1"),
        tool_turn(descriptor.model_name, '{"timezone":"UTC"}', call_id="call-2"),
        final_turn("done"),
    )
    asyncio.run(build_loop(rig).run(completion, load_run(rig, run_id), handle, 0))

    with rig.engine.connect() as connection:
        attempt = connection.execute(
            select(
                JobAttemptRecord.id,
                JobAttemptRecord.worker_id,
                JobAttemptRecord.claim_token,
                JobAttemptRecord.lease_expires_at,
            ).where(JobAttemptRecord.id == handle.attempt_id)
        ).one()
    # The same Attempt, the same Worker and the same token the loop began with.
    assert int(attempt[0]) == handle.attempt_id
    assert attempt[1] == handle.worker_id
    assert bytes(attempt[2]) == handle.claim_token
    assert attempt[3] is not None and attempt[3] > NOW
