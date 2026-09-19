"""Shared harness for the D7 MCP-side failure acceptance suites.

D7 exists because composition is the thing that was never tested. For MCP that gap is sharper than
anywhere else: every pre-D7 failure assertion about a *remotely dispatched* tool call ran against an
in-process built-in, and a built-in cannot be held open mid-flight, cannot outrun a deadline, and
cannot be left dangling while a lease is reclaimed. So the composition that decides a remote call's
fate -- cancel it, expire it, time it out, keep its worker alive -- was unexercised.

Everything below wires the shipped objects: the real discovery over a live loopback MCP server and
the real registry synchronizer, the real gateway and executor, D2's live evaluator, D4's loop, and
the real `JobExecutionService` + `RunExecutor` seam the Worker builds. The only substitutions are
the seams production documents -- a loopback egress policy, a scripted model completion, a sleeper
that returns at once, and a manual clock that moves only when a test moves it.

No test here sleeps for a backoff or a lease duration. A held call is released by the test through
the fake server's gate; a lease expires by reading a later `now`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from nervos_core.application.job_execution import (
    CancellationOutcome,
    ClaimedAttempt,
    JobExecutionService,
)
from nervos_core.application.lease_reclamation import ReclaimedClaim
from nervos_core.application.model_completion import ModelCompletion
from nervos_core.application.run_execution import RunExecutor
from nervos_core.application.tool_invocations import InvocationStatus
from nervos_core.application.trusted_chat import create_builtin_handler_registry
from nervos_core.domain.tools import ToolDescriptor
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobExecutionPersistence,
    SqlAlchemyRunCancellationPersistence,
)
from nervos_core.infrastructure.database.models import JobAttemptRecord
from sqlalchemy import Engine, select, text

from ..support.fake_mcp_server import (
    WRITE_DOCUMENT,
    FakeMcpServer,
    build_server,
    serve_streamable_http,
)
from ..support.loop_harness import (
    ANTHROPIC,
    LATER,
    LEASE,
    NOW,
    LoopRig,
    add_connection,
    agent_instance_id,
    build_rig,
    descriptor_named,
    event_types,
    grant,
    invocation_rows,
    job_row,
    loopback_policy,
    migrate_database,
    no_wait,
    operator_config,
    run_row,
    submit_run,
)

# The C3 recovery backoff is a parameter of the reclamation call, never a wait the test performs.
RECLAIM_BACKOFF = timedelta(seconds=5)
# A heartbeat far shorter than any lease, so a held call spans several renewals without the test
# waiting a real lease duration for anything.
FAST_HEARTBEAT = timedelta(milliseconds=10)


class ManualClock:
    """A logical clock the test advances explicitly, so no assertion races wall time.

    Time is an input to execution, not a consequence of sleeping. A lease is renewed against a
    later `now` because the test moved the clock, which is what makes "the heartbeat stayed
    healthy" observable without waiting for a real lease window to pass.
    """

    def __init__(self, now: datetime = NOW) -> None:
        self._now = now

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta


@dataclass(slots=True)
class McpWorld:
    """One disposable database plus the shipped MCP composition over a live fake server."""

    engine: Engine
    rig: LoopRig
    server: FakeMcpServer
    endpoint: str
    instance_id: int
    descriptors: tuple[ToolDescriptor, ...]

    def descriptor(self, upstream_name: str) -> ToolDescriptor:
        return descriptor_named(self.descriptors, upstream_name)


@asynccontextmanager
async def mcp_world(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    gate: asyncio.Event | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> AsyncGenerator[McpWorld, None]:
    """Stand up the real MCP stack over a fresh database and a live loopback server.

    ``gate`` is handed to the fake server so a call the test chooses can be held open
    deterministically; ``sleep`` is the loop's retry sleeper, which a test injects so an in-Attempt
    ladder costs no wall clock. The gateway is always closed, so no session outlives its test.
    """
    engine = migrate_database(tmp_path, monkeypatch)
    instance_id = agent_instance_id(engine)
    server = build_server(gate=gate)
    async with serve_streamable_http(server) as endpoint:
        config = operator_config(endpoint)
        policy = loopback_policy(endpoint)
        _connection_id, descriptors = await add_connection(
            engine, endpoint=endpoint, config=config, policy=policy
        )
        for descriptor in descriptors:
            grant(engine, tool_definition_id=descriptor.tool_definition_id, instance_id=instance_id)
        rig = build_rig(engine, config=config, policy=policy, sleep=sleep)
        try:
            yield McpWorld(
                engine=engine,
                rig=rig,
                server=server,
                endpoint=endpoint,
                instance_id=instance_id,
                descriptors=descriptors,
            )
        finally:
            await rig.gateway.close_all()


def _fixed_now() -> datetime:
    """The default clock: every operation happens at the rig's frozen `NOW`."""
    return NOW


def build_execution(
    rig: LoopRig,
    *,
    completion: ModelCompletion,
    clock: Callable[[], datetime] = _fixed_now,
    sleeper: Callable[[float], Awaitable[None]] = no_wait,
    heartbeat_interval: timedelta = FAST_HEARTBEAT,
    lease_duration: timedelta = timedelta(minutes=5),
) -> JobExecutionService:
    """Build the Worker's execution service with the loop wired in.

    This is the seam D7 closes: `RunExecutor` receives the `tool_loop`, so a tool-enabled Run is
    routed through it exactly as `apps/worker` routes it. Both the loop's in-Attempt ladder and the
    service's persistence retry use the same immediately-returning sleeper.
    """
    executor = RunExecutor(create_builtin_handler_registry(), tool_loop=rig.loop)
    return JobExecutionService(
        SqlAlchemyJobExecutionPersistence(rig.engine),
        executor,
        {ANTHROPIC: completion},
        clock,
        sleep=sleeper,
        heartbeat_interval=heartbeat_interval,
        lease_duration=lease_duration,
    )


def claim_only(
    engine: Engine,
    run_id: int,
    *,
    worker_id: str = "worker-1",
    now: datetime = NOW,
) -> ClaimedAttempt:
    """Claim the Run's Attempt but never cross the start boundary.

    `JobExecutionService.execute` performs the start itself; starting the Attempt here would make
    that boundary a no-op and stop testing it.
    """
    persistence = SqlAlchemyJobExecutionPersistence(engine)
    handle = persistence.claim_next(
        worker_id=worker_id,
        provider_ids=(ANTHROPIC,),
        max_active=4,
        now=now,
        lease_duration=LEASE,
    )
    assert handle is not None, "the Run was not claimable"
    assert handle.run_id == run_id, "a different Run was claimed"
    return handle


# --- durable truth readers ---------------------------------------------------------------------


def event_rows(engine: Engine, run_id: int) -> list[tuple[str, int | None, str | None]]:
    """Every event on one Run's timeline, in order, as (type, tool_invocation_id, code)."""
    with engine.connect() as connection:
        return [
            (str(row[0]), row[1], row[2])
            for row in connection.execute(
                text(
                    "SELECT event_type, tool_invocation_id, code FROM run_events"
                    " WHERE run_id = :run_id ORDER BY sequence"
                ),
                {"run_id": run_id},
            ).all()
        ]


def tool_events(engine: Engine, run_id: int) -> list[tuple[str, int | None, str | None]]:
    """Only the per-call facts, which are the ones a failure path must not invent."""
    return [event for event in event_rows(engine, run_id) if event[0].startswith("tool.")]


def invocation_states(engine: Engine) -> list[str]:
    """The status of every invocation row, in `tool_sequence` order."""
    return [str(row["status"]) for row in invocation_rows(engine)]


def invocation_row(engine: Engine, *, status: InvocationStatus) -> dict[str, Any]:
    rows = [row for row in invocation_rows(engine) if row["status"] == status.value]
    assert len(rows) == 1, f"expected exactly one {status.value} invocation, found {len(rows)}"
    return rows[0]


def attempt_state(engine: Engine, attempt_id: int) -> dict[str, Any]:
    """The Attempt's liveness columns plus its status, which is what a lease assertion reads."""
    with engine.connect() as connection:
        return dict(
            connection.execute(
                select(
                    JobAttemptRecord.status,
                    JobAttemptRecord.lease_expires_at,
                    JobAttemptRecord.last_heartbeat_at,
                ).where(JobAttemptRecord.id == attempt_id)
            )
            .mappings()
            .one()
        )


def reclaim(engine: Engine, *, now: datetime) -> ReclaimedClaim | None:
    """Invoke the real C3 reclamation path exactly once, as the Worker's loop would."""
    return SqlAlchemyJobExecutionPersistence(engine).reclaim_next_expired_claim(
        now=now, backoff=RECLAIM_BACKOFF
    )


def cancel_run(engine: Engine, run_id: int, *, now: datetime = LATER) -> CancellationOutcome:
    """Invoke the real owner-cancellation path, as the control-plane API would."""
    return SqlAlchemyRunCancellationPersistence(engine).cancel_run(
        user_id=1, run_id=run_id, now=now
    )


async def await_until(
    check: Callable[[], bool],
    *,
    attempts: int = 2000,
    delay: float = 0.005,
) -> None:
    """Wait for a condition a held call makes true, without ever racing a fixed sleep.

    The bound is a bounded poll, not a backoff: it exists so a broken composition fails a test
    instead of hanging, and it is reached only when the durable state or the server ledger already
    says the thing the test is waiting for.
    """
    for _ in range(attempts):
        if check():
            return
        await asyncio.sleep(delay)
    raise AssertionError("the awaited condition was not reached within the bounded wait")


__all__ = [
    "FAST_HEARTBEAT",
    "LATER",
    "LEASE",
    "NOW",
    "RECLAIM_BACKOFF",
    "WRITE_DOCUMENT",
    "ManualClock",
    "McpWorld",
    "attempt_state",
    "await_until",
    "build_execution",
    "cancel_run",
    "claim_only",
    "event_rows",
    "event_types",
    "invocation_row",
    "invocation_states",
    "job_row",
    "mcp_world",
    "reclaim",
    "run_row",
    "submit_run",
    "tool_events",
]
