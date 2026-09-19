"""D6 tool reconciliation: what the recovery engine *commits* for an abandoned Attempt.

When a Worker dies holding a claim, C3 reclaims the lease and closes the Run. D6 adds one more
obligation to that path: the `tool_invocations` rows the dead Worker left behind must be brought to
a truthful terminal state in the same transaction that terminalizes the Attempt, because that table
is authoritative about what a call did.

The subject of this suite is therefore the committed result, not the intent: every test drives the
real fenced persistence (and, for the end-to-end cases, the real D4 loop), expires a real lease
through the real C3 reclamation, and then reads the durable rows and the Run Event chronology back.

Three frozen rules are what the tests pin down:

* a `requested` call provably never dispatched, so it is closed `cancelled` with no tool event;
* a `started` call may already have reached an external system, so it is closed `ambiguous` with
  `tool_outcome_unknown` and gets exactly one `tool.ambiguous` event;
* a call already terminal is left byte-for-byte alone, and nothing is ever replayed -- the Run ends
  `failed` with `execution_outcome_ambiguous` exactly once regardless of how many times recovery
  runs.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from d6_support import (
    LEASE,
    NOW,
    Rig,
    ScriptedCompletion,
    build_loop,
    build_rig,
    claim,
    descriptor_named,
    event_sequences,
    event_types,
    grant,
    invitations,
    load_run,
    reclaim,
    run_row,
    submit,
    tool_events,
    tool_turn,
)
from nervos_core.application.builtin_tools import BUILTIN_SOURCE_REF, BuiltinToolSource
from nervos_core.application.model_completion import (
    EXECUTION_OUTCOME_AMBIGUOUS,
    TOOL_OUTCOME_UNKNOWN,
    safe_error_message,
)
from nervos_core.application.tool_invocations import (
    InvocationRequest,
    InvocationStatus,
    RecordOutcomeKind,
    RefusalOutcomeKind,
    StartOutcomeKind,
    result_envelope,
)
from nervos_core.application.tool_permissions import PermissionDecision
from nervos_core.application.tool_registry import ToolRegistry
from nervos_core.domain.runs import RunStatus
from nervos_core.infrastructure.database.jobs import SqlAlchemyJobExecutionPersistence
from nervos_core.infrastructure.database.models import ToolInvocationRecord
from nervos_core.infrastructure.database.tool_definitions import (
    SqlAlchemyToolDefinitionPersistence,
)
from nervos_core.infrastructure.database.tool_invocations import (
    SqlAlchemyToolInvocationPersistence,
)
from sqlalchemy import select

# One second past `NOW + LEASE`, so a claim taken at `NOW` is provably expired and reclaimable.
EXPIRED = NOW + LEASE + timedelta(seconds=1)

TOOL = "calculate"
ARGUMENTS: dict[str, Any] = {"expression": "1+1"}


@dataclass(frozen=True, slots=True)
class _World:
    """One claimed, started tool-enabled Run, plus the descriptor its calls target."""

    rig: Rig
    descriptor: Any
    run_id: int
    handle: Any


def _world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, name: str) -> _World:
    """A granted tool, a submitted Run, and a real claimed Attempt past the start boundary."""
    rig = build_rig(tmp_path, monkeypatch, name=name)
    descriptor = descriptor_named(rig, TOOL)
    # The grant must exist before submission so the Run's snapshot cutoff includes it.
    grant(rig, descriptor=descriptor)
    run_id = submit(rig)
    handle = claim(rig, run_id)
    return _World(rig=rig, descriptor=descriptor, run_id=run_id, handle=handle)


def _record_requested(
    world: _World,
    *,
    sequence: int,
    call_id: str,
) -> int:
    """Write a real `requested` row through the real fenced persistence.

    This is the exact call the loop's `_dispatch` makes; it is invoked here directly so a test can
    stop *between* the request write and the start-byte write, which is the durable state a Worker
    that died after recording intent but before dispatching would leave behind.
    """
    handle = world.handle
    outcome = SqlAlchemyToolInvocationPersistence(world.rig.engine).record_requested(
        claim=handle,
        request=InvocationRequest(
            run_id=handle.run_id,
            job_id=handle.job_id,
            attempt_id=handle.attempt_id,
            tool_sequence=sequence,
            tool_definition_id=world.descriptor.tool_definition_id,
            source_kind=world.descriptor.source_kind,
            source_id=world.descriptor.source_id,
            upstream_name=world.descriptor.upstream_name,
            model_name=world.descriptor.model_name,
            definition_fingerprint=world.descriptor.fingerprint,
            provider_call_id=call_id,
            permission_decision=PermissionDecision(allowed=True),
            arguments=ARGUMENTS,
        ),
        now=NOW,
    )
    assert outcome.kind is RecordOutcomeKind.REQUESTED
    assert outcome.invocation_id is not None
    return outcome.invocation_id


def _start(world: _World, invocation_id: int) -> None:
    """Cross the ambiguity boundary through the real transition, then stop."""
    outcome = SqlAlchemyToolInvocationPersistence(world.rig.engine).mark_started(
        claim=world.handle, invocation_id=invocation_id, now=NOW
    )
    assert outcome.kind is StartOutcomeKind.STARTED


def _invocation_row(rig: Rig, invocation_id: int) -> dict[str, Any]:
    with rig.engine.connect() as connection:
        return dict(
            connection.execute(
                select(ToolInvocationRecord).where(ToolInvocationRecord.id == invocation_id)
            )
            .mappings()
            .one()
        )


def _terminal(row: dict[str, Any]) -> bool:
    return row["status"] not in (
        InvocationStatus.REQUESTED.value,
        InvocationStatus.STARTED.value,
    )


def test_a_requested_invocation_left_by_a_lost_worker_is_cancelled_with_no_tool_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recorded-but-never-dispatched call is `cancelled`, keeps its decision, and stays silent."""
    world = _world(tmp_path, monkeypatch, name="requested.db")
    invocation_id = _record_requested(world, sequence=1, call_id="call-1")
    before = _invocation_row(world.rig, invocation_id)
    assert before["status"] == InvocationStatus.REQUESTED.value
    assert before["started_at"] is None

    reclaimed = reclaim(world.rig, now=EXPIRED)
    assert reclaimed is not None

    after = _invocation_row(world.rig, invocation_id)
    assert after["status"] == InvocationStatus.CANCELLED.value
    assert after["started_at"] is None
    assert after["finished_at"] is not None
    # The permission decision recorded when the call was requested is audit evidence; recovery may
    # not rewrite it.
    assert after["permission_decision"] == before["permission_decision"]
    # A cancelled call emits no tool event: the six frozen tool event types have no
    # `tool.cancelled`, so the absence of `tool.ambiguous` is the whole assertion.
    assert tool_events(world.rig, world.run_id) == [("tool.requested", invocation_id, None)]


def test_a_started_invocation_left_by_a_lost_worker_is_ambiguous_and_emits_tool_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A call that may have landed is `ambiguous`, never `failed`, and gets one linked event."""
    world = _world(tmp_path, monkeypatch, name="started.db")
    invocation_id = _record_requested(world, sequence=1, call_id="call-1")
    _start(world, invocation_id)

    reclaimed = reclaim(world.rig, now=EXPIRED)
    assert reclaimed is not None

    after = _invocation_row(world.rig, invocation_id)
    assert after["status"] == InvocationStatus.AMBIGUOUS.value
    assert after["error_code"] == TOOL_OUTCOME_UNKNOWN
    assert after["error_message"] == safe_error_message(TOOL_OUTCOME_UNKNOWN)
    assert after["finished_at"] is not None
    events = tool_events(world.rig, world.run_id)
    assert events[-1] == ("tool.ambiguous", invocation_id, TOOL_OUTCOME_UNKNOWN)
    assert [event[0] for event in events] == [
        "tool.requested",
        "tool.started",
        "tool.ambiguous",
    ]


def test_recovery_event_order_places_tool_ambiguity_before_the_run_terminal_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The frozen tail is attempt.expired, recovery.ambiguous, tool.ambiguous, run.failed."""
    world = _world(tmp_path, monkeypatch, name="order.db")
    invocation_id = _record_requested(world, sequence=1, call_id="call-1")
    _start(world, invocation_id)

    assert reclaim(world.rig, now=EXPIRED) is not None

    assert event_types(world.rig, world.run_id)[-4:] == [
        "attempt.expired",
        "recovery.ambiguous",
        "tool.ambiguous",
        "run.failed",
    ]
    sequences = event_sequences(world.rig, world.run_id)
    tool_index = event_types(world.rig, world.run_id).index("tool.ambiguous")
    run_index = event_types(world.rig, world.run_id).index("run.failed")
    assert sequences[tool_index] < sequences[run_index]
    run = run_row(world.rig, world.run_id)
    assert run["status"] == RunStatus.FAILED.value
    assert run["error_code"] == EXECUTION_OUTCOME_AMBIGUOUS


def test_completed_work_is_never_reinterpreted_by_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A succeeded call is left byte-identical; only the dangling sibling is reconciled."""
    world = _world(tmp_path, monkeypatch, name="terminal.db")
    persistence = SqlAlchemyToolInvocationPersistence(world.rig.engine)
    succeeded_id = _record_requested(world, sequence=1, call_id="call-1")
    _start(world, succeeded_id)
    assert persistence.mark_succeeded(
        claim=world.handle,
        invocation_id=succeeded_id,
        envelope=result_envelope(text="the result", structured=None),
        now=NOW,
    )
    dangling_id = _record_requested(world, sequence=2, call_id="call-2")
    _start(world, dangling_id)
    snapshot = _invocation_row(world.rig, succeeded_id)

    assert reclaim(world.rig, now=EXPIRED) is not None

    # Recovering the Attempt may not rewrite a conclusion a Worker already committed.
    assert _invocation_row(world.rig, succeeded_id) == snapshot
    ambiguous_events = [
        event for event in tool_events(world.rig, world.run_id) if event[0] == "tool.ambiguous"
    ]
    assert len(ambiguous_events) == 1
    assert ambiguous_events[0][1] == dangling_id


def test_recovery_is_idempotent_and_reconciles_nothing_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second reclaim finds no eligible candidate and adds no second fact."""
    world = _world(tmp_path, monkeypatch, name="idempotent.db")
    invocation_id = _record_requested(world, sequence=1, call_id="call-1")
    _start(world, invocation_id)

    assert reclaim(world.rig, now=EXPIRED) is not None
    # The Job and Run are terminal after the first pass, so nothing is eligible a second time.
    assert reclaim(world.rig, now=EXPIRED) is None

    rows = invitations(world.rig)
    assert len(rows) == 1
    assert _terminal(rows[0])
    assert rows[0]["status"] == InvocationStatus.AMBIGUOUS.value
    assert [
        event for event in tool_events(world.rig, world.run_id) if event[0] == "tool.ambiguous"
    ] == [("tool.ambiguous", invocation_id, TOOL_OUTCOME_UNKNOWN)]


def test_a_live_lease_is_never_reclaimed_and_reconciles_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A healthy long tool call keeps its lease, its `started` row, and its silence."""
    world = _world(tmp_path, monkeypatch, name="live.db")
    invocation_id = _record_requested(world, sequence=1, call_id="call-1")
    _start(world, invocation_id)

    # `now == NOW` is inside the lease that `claim` took at `NOW`, so no candidate is eligible.
    assert reclaim(world.rig, now=NOW) is None

    assert _invocation_row(world.rig, invocation_id)["status"] == InvocationStatus.STARTED.value
    assert [
        event for event in tool_events(world.rig, world.run_id) if event[0] == "tool.ambiguous"
    ] == []


class _CountingEffectExecutor:
    """A stand-in for a remote tool: the counter is the external side effect.

    The built-ins are in-process and side-effect-free, so the acceptance property -- that a call
    which may already have landed is never re-dispatched -- is modelled by a counter incremented
    exactly when a real executor would have performed its work. Raising `CancelledError` reproduces
    the loop's own documented "caller withdrew the task" path, leaving the invocation `started`.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, descriptor: Any, arguments: Any) -> Any:
        self.calls += 1
        raise asyncio.CancelledError


def _registry_with(rig: Rig, executor: Any) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        source_ref=BUILTIN_SOURCE_REF,
        source=BuiltinToolSource(SqlAlchemyToolDefinitionPersistence(rig.engine)),
        executor=executor,
    )
    return registry


def test_a_remote_effect_happens_exactly_once_and_recovery_never_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No replay: the effect lands once, the Run fails ambiguous, and the loop is not re-entered."""
    world = _world(tmp_path, monkeypatch, name="once.db")
    executor = _CountingEffectExecutor()
    completion = ScriptedCompletion(tool_turn(world.descriptor.model_name, '{"expression":"2+2"}'))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            build_loop(world.rig, registry=_registry_with(world.rig, executor)).run(
                completion, load_run(world.rig, world.run_id), world.handle, 0
            )
        )

    # The Worker died after the dispatch but before any terminal write.
    assert executor.calls == 1
    assert len(completion.requests) == 1
    rows = invitations(world.rig)
    assert len(rows) == 1
    assert rows[0]["status"] == InvocationStatus.STARTED.value
    invocation_id = int(rows[0]["id"])

    assert reclaim(world.rig, now=EXPIRED) is not None

    # The effect happened once and recovery added no second attempt at it: the counter is still 1,
    # the model was still asked exactly once, and there is still exactly one invocation row.
    assert executor.calls == 1
    assert len(completion.requests) == 1
    assert len(invitations(world.rig)) == 1
    assert _invocation_row(world.rig, invocation_id)["status"] == InvocationStatus.AMBIGUOUS.value
    run = run_row(world.rig, world.run_id)
    assert run["status"] == RunStatus.FAILED.value
    assert run["error_code"] == EXECUTION_OUTCOME_AMBIGUOUS


def test_pre_start_reclamation_reconciles_no_tool_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A death before the start boundary leaves nothing open, so recovery touches nothing."""
    rig = build_rig(tmp_path, monkeypatch, name="prestart.db")
    descriptor = descriptor_named(rig, TOOL)
    grant(rig, descriptor=descriptor)
    run_id = submit(rig)
    # Claim but never start: the Attempt has no `execution_started_at`, so no call can exist.
    handle = SqlAlchemyJobExecutionPersistence(rig.engine).claim_next(
        worker_id="worker-1",
        provider_ids=("anthropic",),
        max_active=4,
        now=NOW,
        lease_duration=LEASE,
    )
    assert handle is not None
    assert handle.run_id == run_id

    assert reclaim(rig, now=EXPIRED) is not None

    assert tool_events(rig, run_id) == []
    assert invitations(rig) == []


def test_a_worker_that_lost_its_claim_cannot_append_a_tool_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the claim is reclaimed, a stale Worker's refusal write is fenced out entirely."""
    world = _world(tmp_path, monkeypatch, name="fenced.db")
    assert reclaim(world.rig, now=EXPIRED) is not None
    before = tool_events(world.rig, world.run_id)

    outcome = SqlAlchemyToolInvocationPersistence(world.rig.engine).record_pre_dispatch_refusal(
        claim=world.handle, now=EXPIRED
    )

    assert outcome.kind is RefusalOutcomeKind.FENCED
    # A fenced write mutates nothing -- not even the NULL-linked denial event a live claim
    # would have received.
    assert tool_events(world.rig, world.run_id) == before
