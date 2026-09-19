"""Shared rig for the D6 audit, recovery and cancellation suites.

Every D6 suite needs the same world: a migrated database holding the reconciled D3 built-ins, one
granted tool, a submitted tool-enabled Run with a real claimed Attempt, and a loop wired to the
real durable persistence. Building that once here keeps each suite's test bodies about the fact it
proves rather than about the fixture.

Nothing here substitutes for a production seam. The claim, the start boundary, the invocation
writes, the recovery and the cancellation all run through the real implementations -- that is the
whole point of these suites, because D6's subject is what the engine *commits*.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from execution_support import migrate
from nervos_core.application.builtin_tools import (
    BUILTIN_SOURCE_REF,
    builtin_tool_specs,
    create_builtin_tool_registry,
    reconcile_builtin_definitions,
)
from nervos_core.application.model_completion import (
    ModelResponse,
    ModelUsage,
    StopOutcome,
    ToolCall,
)
from nervos_core.application.tool_invocations import InvocationStatus
from nervos_core.application.tool_loop import ToolLoop
from nervos_core.application.tool_permissions import PermissionDecision, PermissionDenialReason
from nervos_core.application.tool_registry import ToolRegistry
from nervos_core.application.trusted_chat import NERVOS_TOOL_CHAT_SYSTEM_INSTRUCTION
from nervos_core.domain.runs import TOOL_ENABLED_LIMITS
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobExecutionPersistence,
    SqlAlchemyJobPersistence,
)
from nervos_core.infrastructure.database.models import (
    AgentInstanceRecord,
    AgentToolGrantRecord,
    RunRecord,
    ToolDefinitionRecord,
    ToolInvocationRecord,
)
from nervos_core.infrastructure.database.tool_definitions import (
    SqlAlchemyToolDefinitionPersistence,
)
from nervos_core.infrastructure.database.tool_invocations import (
    SqlAlchemyToolInvocationPersistence,
)
from nervos_core.infrastructure.database.tools import (
    SqlAlchemyToolPermissionEvaluator,
    SqlAlchemyToolPermissionPersistence,
)
from sqlalchemy import Engine, delete, func, insert, select, text

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
LEASE = timedelta(minutes=2)
RECLAIM_BACKOFF = timedelta(seconds=5)


@dataclass(frozen=True, slots=True)
class Rig:
    """One tool-enabled world: its Engine, its descriptors, and its submission helpers."""

    engine: Engine
    descriptors: tuple[Any, ...]
    instance_id: int


def build_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, name: str = "d6.db") -> Rig:
    """Migrate a disposable database and reconcile the D3 built-in definitions into it.

    Reconciliation runs for the same reason the Worker runs it at startup: a tool the loop can
    execute has a durable definition before any Run could name it. It grants nothing.
    """
    engine = migrate(tmp_path / name, monkeypatch, agents=1)
    definitions = SqlAlchemyToolDefinitionPersistence(engine)
    descriptors = reconcile_builtin_definitions(
        definitions, specs=builtin_tool_specs(clock=lambda: NOW), now=NOW
    )
    with engine.connect() as connection:
        instance_id = int(connection.execute(select(AgentInstanceRecord.id)).scalar_one())
    return Rig(engine=engine, descriptors=tuple(descriptors), instance_id=instance_id)


def descriptor_named(rig: Rig, upstream_name: str) -> Any:
    return next(d for d in rig.descriptors if d.upstream_name == upstream_name)


def grant(rig: Rig, *, descriptor: Any, now: datetime = NOW) -> int:
    """Grant one tool to the rig's Agent Instance, as a user's review would record it."""
    with rig.engine.begin() as connection:
        fingerprint = connection.execute(
            select(ToolDefinitionRecord.fingerprint).where(
                ToolDefinitionRecord.id == descriptor.tool_definition_id
            )
        ).scalar_one()
        connection.execute(
            insert(AgentToolGrantRecord).values(
                agent_instance_id=rig.instance_id,
                tool_definition_id=descriptor.tool_definition_id,
                reviewed_fingerprint=fingerprint,
                created_at=now,
            )
        )
    with rig.engine.connect() as connection:
        return int(connection.execute(select(func.max(AgentToolGrantRecord.id))).scalar_one())


def revoke(rig: Rig, *, descriptor: Any) -> None:
    """Withdraw one grant durably, without touching anything the Worker has cached."""
    with rig.engine.begin() as connection:
        connection.execute(
            delete(AgentToolGrantRecord).where(
                AgentToolGrantRecord.tool_definition_id == descriptor.tool_definition_id,
                AgentToolGrantRecord.agent_instance_id == rig.instance_id,
            )
        )


def submit(rig: Rig, *, limits: Any = TOOL_ENABLED_LIMITS, now: datetime = NOW) -> int:
    """Submit through the real durable submission path, so the snapshot is the real one."""
    run = SqlAlchemyJobPersistence(rig.engine).submit(
        owner_user_id=1,
        agent_instance_id=rig.instance_id,
        input_text="do the thing",
        limits=limits,
        now=now,
    )
    return run.id


def claim(rig: Rig, run_id: int, *, worker_id: str = "worker-1", now: datetime = NOW) -> Any:
    """Claim and start the Run's Attempt through the real engine, returning its claim handle."""
    persistence = SqlAlchemyJobExecutionPersistence(rig.engine)
    handle = persistence.claim_next(
        worker_id=worker_id,
        provider_ids=("anthropic",),
        max_active=4,
        now=now,
        lease_duration=LEASE,
    )
    assert handle is not None
    assert handle.run_id == run_id
    assert persistence.start_attempt(handle, now=now) is True
    return handle


def load_run(rig: Rig, run_id: int) -> Any:
    return SqlAlchemyJobExecutionPersistence(rig.engine).load_run(run_id)


def build_loop(
    rig: Rig,
    *,
    registry: ToolRegistry | None = None,
    authorize: Any | None = None,
) -> ToolLoop:
    """The real D4 loop over the real durable seams, with a frozen clock.

    `authorize` is injectable because a test that must see the *second*, in-transaction permission
    check disagree with the first needs to control when the live predicate starts refusing. The
    default is the production evaluator, so every other test runs the real one.
    """
    definitions = SqlAlchemyToolDefinitionPersistence(rig.engine)
    return ToolLoop(
        registry=registry or create_builtin_tool_registry(definitions, clock=lambda: NOW),
        source_ref=BUILTIN_SOURCE_REF,
        authorize=authorize or SqlAlchemyToolPermissionEvaluator(rig.engine),
        invocations=SqlAlchemyToolInvocationPersistence(rig.engine),
        usage=SqlAlchemyJobExecutionPersistence(rig.engine),
        system_instruction=NERVOS_TOOL_CHAT_SYSTEM_INSTRUCTION,
        clock=lambda: NOW,
    )


def tool_turn(model_name: str, arguments_json: str, *, call_id: str = "call-1") -> ModelResponse:
    return ModelResponse(
        "",
        "anthropic",
        "opaque/model",
        StopOutcome.TOOL_USE,
        ModelUsage(1, 1, 2),
        (ToolCall(call_id=call_id, name=model_name, arguments_json=arguments_json),),
    )


def tool_turns(*calls: tuple[str, str, str]) -> ModelResponse:
    """One assistant turn carrying several calls, in provider order."""
    return ModelResponse(
        "",
        "anthropic",
        "opaque/model",
        StopOutcome.TOOL_USE,
        ModelUsage(1, 1, 2),
        tuple(
            ToolCall(call_id=call_id, name=model_name, arguments_json=arguments_json)
            for call_id, model_name, arguments_json in calls
        ),
    )


def final_turn(text_value: str = "the answer") -> ModelResponse:
    return ModelResponse(
        text_value, "anthropic", "opaque/model", StopOutcome.STOP, ModelUsage(1, 1, 2)
    )


class ScriptedCompletion:
    """A provider completion that returns pre-scripted responses and records what it was offered."""

    def __init__(self, *responses: ModelResponse) -> None:
        self._responses = list(responses)
        self.requests: list[Any] = []

    async def complete(self, request: Any) -> ModelResponse:
        self.requests.append(request)
        return self._responses.pop(0)


def events(rig: Rig, run_id: int) -> list[tuple[str, int | None, str | None]]:
    """Every event on one Run's timeline, in order, as (type, tool_invocation_id, code)."""
    with rig.engine.connect() as connection:
        return [
            (row[0], row[1], row[2])
            for row in connection.execute(
                text(
                    "SELECT event_type, tool_invocation_id, code FROM run_events"
                    " WHERE run_id = :r ORDER BY sequence"
                ),
                {"r": run_id},
            ).all()
        ]


def tool_events(rig: Rig, run_id: int) -> list[tuple[str, int | None, str | None]]:
    return [event for event in events(rig, run_id) if event[0].startswith("tool.")]


def event_types(rig: Rig, run_id: int) -> list[str]:
    return [event[0] for event in events(rig, run_id)]


def event_sequences(rig: Rig, run_id: int) -> list[int]:
    with rig.engine.connect() as connection:
        return [
            int(value)
            for value in connection.execute(
                text("SELECT sequence FROM run_events WHERE run_id = :r ORDER BY sequence"),
                {"r": run_id},
            ).scalars()
        ]


def invitations(rig: Rig) -> list[dict[str, Any]]:
    """Every durable invocation row, in the order the Attempt requested it."""
    with rig.engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                select(ToolInvocationRecord).order_by(ToolInvocationRecord.tool_sequence)
            )
            .mappings()
            .all()
        ]


def invocation_named(rig: Rig, *, status: InvocationStatus) -> dict[str, Any]:
    rows = [row for row in invitations(rig) if row["status"] == status.value]
    assert len(rows) == 1, f"expected exactly one {status.value} invocation, found {len(rows)}"
    return rows[0]


def run_row(rig: Rig, run_id: int) -> dict[str, Any]:
    with rig.engine.connect() as connection:
        return dict(
            connection.execute(select(RunRecord).where(RunRecord.id == run_id)).mappings().one()
        )


def reclaim(rig: Rig, *, now: datetime) -> Any:
    """Invoke the real C3 reclamation path exactly once, as the Worker's loop would."""
    return SqlAlchemyJobExecutionPersistence(rig.engine).reclaim_next_expired_claim(
        now=now, backoff=RECLAIM_BACKOFF
    )


def cancel(rig: Rig, run_id: int, *, now: datetime) -> Any:
    """Invoke the real C5 cancellation path, as the owner's API call would."""
    from nervos_core.infrastructure.database.jobs import SqlAlchemyRunCancellationPersistence

    return SqlAlchemyRunCancellationPersistence(rig.engine).cancel_run(
        user_id=1, run_id=run_id, now=now
    )


def permission(rig: Rig, *, run_id: int, tool_definition_id: int) -> tuple[bool, str | None]:
    """The live D2 verdict for one call, as (allowed, reason-name-or-None)."""
    evaluator = SqlAlchemyToolPermissionEvaluator(rig.engine)
    decision = evaluator.check_permission(run_id=run_id, tool_definition_id=tool_definition_id)
    if decision.allowed:
        return (True, None)
    reason = decision.reason
    return (False, None if reason is None else reason.name)


__all__ = [
    "LATER",
    "LEASE",
    "NOW",
    "RECLAIM_BACKOFF",
    "PermissionDecision",
    "PermissionDenialReason",
    "Rig",
    "ScriptedCompletion",
    "SqlAlchemyToolPermissionPersistence",
    "build_loop",
    "build_rig",
    "cancel",
    "claim",
    "descriptor_named",
    "event_sequences",
    "event_types",
    "events",
    "final_turn",
    "grant",
    "invitations",
    "invocation_named",
    "load_run",
    "permission",
    "reclaim",
    "revoke",
    "run_row",
    "submit",
    "tool_events",
    "tool_turn",
    "tool_turns",
]
