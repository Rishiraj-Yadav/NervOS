"""Shared harness for the D7 integrated Stage-D acceptance suites.

D7 proves composition, so the one thing this module must not do is substitute for a production
seam. Everything below wires the *real* modules D1-D6 shipped -- the durable submission primitive,
the fenced execution persistence, D2's live evaluator, D3's registry, D4's loop, and D6's audit
writes -- and nothing here re-implements a transition.

The gap this closes is specific and was measured rather than assumed: every pre-D7 test in the
repository constructs `RunExecutor` **without** a `tool_loop`, so `run_execution.py`'s routing
decision (which shape a Run takes) and its fail-closed branch have never been exercised by a test.
`build_execution` builds the composition the Worker builds, so a D7 journey crosses that seam.

Time is a parameter, never a wait: a lease expires by passing a later `now`, a retry ladder
exhausts through an injected sleeper, and no acceptance test sleeps for a real backoff.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from d6_support import (
    LATER,
    LEASE,
    NOW,
    RECLAIM_BACKOFF,
    Rig,
    ScriptedCompletion,
    build_rig,
    descriptor_named,
    event_sequences,
    event_types,
    events,
    final_turn,
    grant,
    invitations,
    invocation_named,
    load_run,
    reclaim,
    revoke,
    run_row,
    submit,
    tool_events,
    tool_turn,
    tool_turns,
)
from nervos_core.application.builtin_tools import BUILTIN_SOURCE_REF, create_builtin_tool_registry
from nervos_core.application.job_execution import ClaimedAttempt, JobExecutionService
from nervos_core.application.model_completion import ModelCompletion
from nervos_core.application.run_execution import ExecutionOutcome, RunExecutor
from nervos_core.application.tool_catalog import ToolSourceSynchronizer
from nervos_core.application.tool_loop import ToolLoop
from nervos_core.application.tool_permissions import PermissionDecision, PermissionDenialReason
from nervos_core.application.tool_registry import ToolRegistry
from nervos_core.application.trusted_chat import (
    NERVOS_TOOL_CHAT_SYSTEM_INSTRUCTION,
    create_builtin_handler_registry,
)
from nervos_core.domain.runs import STAGE_B_LIMITS, TOOL_ENABLED_LIMITS, Run, RunLimits
from nervos_core.infrastructure.database.jobs import SqlAlchemyJobExecutionPersistence
from nervos_core.infrastructure.database.models import (
    JobAttemptRecord,
    JobRecord,
    RunRecord,
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
from sqlalchemy import Engine, func, select, text

PROVIDER_ID = "anthropic"


async def no_wait(_seconds: float) -> None:
    """A sleeper that returns at once, so a backoff is deterministic instead of slow."""


class RecordingSleeper:
    """Record every delay a retry ladder asked for, without ever waiting for one.

    The in-Attempt ladder's delays are part of the frozen policy, so a test that only proved the
    ladder *eventually* exhausted would miss a policy change that made it wait three times as long.
    Recording the requested delays asserts the schedule itself while costing no wall clock.
    """

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


@dataclass(frozen=True, slots=True)
class StageD:
    """One tool-enabled world plus the execution composition the Worker would build for it."""

    rig: Rig
    execution: JobExecutionService
    loop: ToolLoop
    registry: ToolRegistry
    completion: ModelCompletion
    sleeper: Callable[[float], Awaitable[None]]

    @property
    def engine(self) -> Engine:
        return self.rig.engine

    @property
    def descriptors(self) -> tuple[Any, ...]:
        return self.rig.descriptors

    @property
    def instance_id(self) -> int:
        return self.rig.instance_id


def build_execution(
    rig: Rig,
    *,
    registry: ToolRegistry | None = None,
    synchronizer: ToolSourceSynchronizer | None = None,
    completion: ModelCompletion | None = None,
    clock: Callable[[], datetime] | None = None,
    sleeper: Callable[[float], Awaitable[None]] | None = None,
) -> StageD:
    """Build the real execution composition, with a `tool_loop` wired in.

    This is the seam no pre-D7 test crossed. `RunExecutor` receives both the tool-free handler
    resolver and the loop, so a Run's own snapshot decides which shape it takes -- exactly as
    `apps/worker/src/nervos_worker/app.py` decides it in production.
    """
    now = clock or (lambda: NOW)
    sleep = sleeper or no_wait
    definitions = SqlAlchemyToolDefinitionPersistence(rig.engine)
    tool_registry = registry or create_builtin_tool_registry(definitions, clock=now)
    persistence = SqlAlchemyJobExecutionPersistence(rig.engine)
    loop = ToolLoop(
        registry=tool_registry,
        source_ref=BUILTIN_SOURCE_REF,
        authorize=SqlAlchemyToolPermissionEvaluator(rig.engine),
        invocations=SqlAlchemyToolInvocationPersistence(rig.engine),
        usage=persistence,
        system_instruction=NERVOS_TOOL_CHAT_SYSTEM_INSTRUCTION,
        clock=now,
        sleep=sleep,
        synchronizer=synchronizer,
    )
    scripted = completion or ScriptedCompletion(final_turn("no scripted response was provided"))
    execution = JobExecutionService(
        persistence,
        RunExecutor(create_builtin_handler_registry(), tool_loop=loop),
        {PROVIDER_ID: scripted},
        now,
        sleep=sleep,
    )
    return StageD(
        rig=rig,
        execution=execution,
        loop=loop,
        registry=tool_registry,
        completion=scripted,
        sleeper=sleep,
    )


def claim_only(
    rig: Rig,
    run_id: int,
    *,
    worker_id: str = "worker-1",
    now: datetime = NOW,
    max_active: int = 4,
) -> ClaimedAttempt:
    """Claim the Run's Attempt *without* starting it, which is what the service expects.

    `JobExecutionService.execute` performs the start itself, so a test that started the Attempt
    here would make the service's own execution-start boundary a no-op and silently stop testing
    it.
    """
    persistence = SqlAlchemyJobExecutionPersistence(rig.engine)
    handle = persistence.claim_next(
        worker_id=worker_id,
        provider_ids=(PROVIDER_ID,),
        max_active=max_active,
        now=now,
        lease_duration=LEASE,
    )
    assert handle is not None, "the Run was not claimable"
    assert handle.run_id == run_id, "a different Run was claimed"
    return handle


async def execute_run(
    stage: StageD,
    run_id: int,
    *,
    worker_id: str = "worker-1",
    now: datetime = NOW,
) -> ExecutionOutcome | None:
    """Take one Run from claim through the real service to its terminal outcome."""
    return await stage.execution.execute(
        claim_only(stage.rig, run_id, worker_id=worker_id, now=now)
    )


def tool_metadata(stage: StageD) -> dict[str, Any]:
    """The durable truth about every tool call one world recorded, read straight from the tables.

    Deliberately raw rather than through the public projection: these are the assertions about what
    the engine *committed*, whereas the public-timeline assertions elsewhere in D7 go through the
    read path on purpose. Keeping the two separate is what makes a projection bug visible instead
    of hiding it behind a shared reader.
    """
    with stage.engine.connect() as connection:
        invocations = connection.execute(
            text(
                "SELECT status, tool_sequence, permission_decision, tool_definition_id,"
                " started_at, output_text FROM tool_invocations ORDER BY tool_sequence"
            )
        ).all()
        total = int(
            connection.execute(text("SELECT count(*) FROM tool_invocations")).scalar_one() or 0
        )
    return {"invocations": list(invocations), "count": total}


def job_and_attempt_counts(rig: Rig, run_id: int) -> tuple[int, int, int]:
    """Return (runs, jobs, attempts) for one Run, which must stay (1, 1, 1) for a whole loop."""
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


__all__ = [
    "LATER",
    "LEASE",
    "NOW",
    "PROVIDER_ID",
    "RECLAIM_BACKOFF",
    "STAGE_B_LIMITS",
    "TOOL_ENABLED_LIMITS",
    "PermissionDecision",
    "PermissionDenialReason",
    "RecordingSleeper",
    "Rig",
    "Run",
    "RunLimits",
    "ScriptedCompletion",
    "SqlAlchemyToolPermissionPersistence",
    "StageD",
    "build_execution",
    "build_rig",
    "claim_only",
    "descriptor_named",
    "event_sequences",
    "event_types",
    "events",
    "execute_run",
    "final_turn",
    "grant",
    "invitations",
    "invocation_named",
    "job_and_attempt_counts",
    "load_run",
    "no_wait",
    "reclaim",
    "revoke",
    "run_row",
    "submit",
    "tool_events",
    "tool_metadata",
    "tool_turn",
    "tool_turns",
]
