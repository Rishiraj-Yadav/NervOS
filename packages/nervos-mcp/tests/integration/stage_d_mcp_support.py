"""Shared harness for the D7 integrated Stage-D acceptance journeys over MCP sources.

D7 exists because no pre-D7 test ever handed a ``tool_loop`` to ``RunExecutor``, so the routing seam
in ``run_execution.py`` -- the branch that sends a tool-enabled Run to D4's loop instead of the
single-call handler -- had never been crossed by a test. Every journey in this package therefore
drives ``JobExecutionService.execute(claimed_attempt)``; that is the seam, and skipping it would
re-create the exact gap this milestone closes.

The composition below is the Worker's, built from the shipped modules: D1's durable submission, the
fenced execution service, D2's live evaluator, D3's registry, D5's gateway/source/executor, the real
``McpRegistrySynchronizer`` from ``nervos_worker.mcp``, and D4's loop. The only test substitutions
are the seams the production modules document as such -- a loopback egress policy (production
refuses plain ``http://`` outright) and a scripted model completion (no provider is reachable).

Time is injected, never awaited: the clock is frozen and the sleeper returns at once, so no journey
waits out a backoff or a lease. ``claim_only`` claims an Attempt **without** starting it, because
``JobExecutionService.execute`` performs the start itself -- a harness that started it here would
make the service's own execution-start boundary a silent no-op.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pytest
from nervos_core.application.builtin_tools import (
    BUILTIN_SOURCE_REF,
    builtin_tool_specs,
    create_builtin_tool_registry,
    reconcile_builtin_definitions,
)
from nervos_core.application.job_execution import ClaimedAttempt, JobExecutionService
from nervos_core.application.mcp_connections import ConnectionTransport
from nervos_core.application.model_completion import ModelCompletion
from nervos_core.application.run_execution import ExecutionOutcome, RunExecutor
from nervos_core.application.tool_loop import ToolLoop
from nervos_core.application.tool_registry import ToolRegistry
from nervos_core.application.trusted_chat import (
    NERVOS_TOOL_CHAT_SYSTEM_INSTRUCTION,
    create_builtin_handler_registry,
)
from nervos_core.domain.tools import ToolDescriptor
from nervos_core.infrastructure.database.jobs import SqlAlchemyJobExecutionPersistence
from nervos_core.infrastructure.database.mcp_connections import (
    SqlAlchemyMcpConnectionPersistence,
)
from nervos_core.infrastructure.database.models import (
    AgentToolGrantRecord,
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
from nervos_core.infrastructure.database.tools import SqlAlchemyToolPermissionEvaluator
from nervos_mcp.gateway import McpGateway
from nervos_mcp.operator_config import McpOperatorConfig
from nervos_mcp.policy.egress import EgressPolicy
from nervos_worker.mcp import McpRegistrySynchronizer, build_mcp_gateway
from sqlalchemy import Engine, func, select, text

from ..support.loop_harness import (
    ANTHROPIC,
    LEASE,
    NOW,
    OWNER_USER_ID,
    ScriptedCompletion,
    discover_connection,
    final_turn,
    no_wait,
)

PROVIDER_ID = ANTHROPIC


@dataclass(slots=True)
class McpStageD:
    """One MCP-enabled world plus the execution composition the Worker would build for it."""

    engine: Engine
    registry: ToolRegistry
    gateway: McpGateway
    synchronizer: McpRegistrySynchronizer
    loop: ToolLoop
    execution: JobExecutionService
    completion: ModelCompletion
    sleeper: Callable[[float], Awaitable[None]]


def build_execution(
    engine: Engine,
    *,
    config: McpOperatorConfig,
    policy: EgressPolicy,
    builtins: bool = False,
    completion: ModelCompletion | None = None,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> McpStageD:
    """Build the real execution composition: real synchroniser plus a ``tool_loop``.

    ``builtins`` reconciles and registers the built-in source exactly as the Worker does at startup;
    the MCP sources are never registered here -- they arrive through
    :meth:`McpRegistrySynchronizer.synchronize_for_run`, the per-Attempt seam these journeys prove.
    """
    now = clock or (lambda: NOW)
    sleeper = sleep or no_wait
    definitions = SqlAlchemyToolDefinitionPersistence(engine)
    if builtins:
        reconcile_builtin_definitions(definitions, specs=builtin_tool_specs(clock=now), now=NOW)
        registry = create_builtin_tool_registry(definitions, clock=now)
    else:
        registry = ToolRegistry()
    connections = SqlAlchemyMcpConnectionPersistence(engine)
    gateway = build_mcp_gateway(connections=connections, operator=config, policy=policy)
    synchronizer = McpRegistrySynchronizer(
        registry=registry,
        connections=connections,
        definitions=definitions,
        gateway=gateway,
    )
    # The persistence layer's retry sleeper is synchronous (it runs inside a transaction retry), so
    # it gets a no-op lambda; the loop and the service get the async sleeper.
    persistence = SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _seconds: None)
    loop = ToolLoop(
        registry=registry,
        source_ref=BUILTIN_SOURCE_REF,
        authorize=SqlAlchemyToolPermissionEvaluator(engine),
        invocations=SqlAlchemyToolInvocationPersistence(engine),
        usage=persistence,
        system_instruction=NERVOS_TOOL_CHAT_SYSTEM_INSTRUCTION,
        clock=now,
        sleep=sleeper,
        synchronizer=synchronizer,
    )
    scripted = completion or ScriptedCompletion(final_turn("no scripted response was provided"))
    execution = JobExecutionService(
        persistence,
        RunExecutor(create_builtin_handler_registry(), tool_loop=loop),
        {PROVIDER_ID: scripted},
        now,
        sleep=sleeper,
    )
    return McpStageD(
        engine=engine,
        registry=registry,
        gateway=gateway,
        synchronizer=synchronizer,
        loop=loop,
        execution=execution,
        completion=scripted,
        sleeper=sleeper,
    )


def claim_only(
    engine: Engine,
    run_id: int,
    *,
    worker_id: str = "worker-1",
    now: datetime = NOW,
    max_active: int = 4,
) -> ClaimedAttempt:
    """Claim the Run's Attempt *without* starting it, which is what the service expects.

    ``JobExecutionService.execute`` starts the Attempt itself, so starting it here would make the
    service's own execution-start boundary a no-op and quietly stop testing it.
    """
    persistence = SqlAlchemyJobExecutionPersistence(engine)
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
    stage: McpStageD,
    run_id: int,
    *,
    worker_id: str = "worker-1",
    now: datetime = NOW,
) -> ExecutionOutcome | None:
    """Take one Run from claim through the real service to its terminal outcome."""
    return await stage.execution.execute(
        claim_only(stage.engine, run_id, worker_id=worker_id, now=now)
    )


def run_events(engine: Engine, run_id: int) -> list[tuple[str, int | None, str | None]]:
    """Every event on one Run's timeline, in order, as (type, tool_invocation_id, code).

    Read straight from ``run_events`` rather than through the public projection: these journeys
    assert what the engine *committed*, so a projection bug stays visible instead of hiding behind a
    shared reader.
    """
    with engine.connect() as connection:
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


def tool_events(engine: Engine, run_id: int) -> list[tuple[str, int | None, str | None]]:
    """Only the ``tool.*`` events, in order, so a journey can assert the audit shape exactly."""
    return [event for event in run_events(engine, run_id) if event[0].startswith("tool.")]


def builtin_descriptors(engine: Engine, *, now: datetime = NOW) -> tuple[ToolDescriptor, ...]:
    """Reconcile the built-in definitions and return their durable descriptors.

    A journey that must *grant* a built-in needs its durable id before the composition is built.
    Reconciliation is idempotent, so the composition's own startup reconcile becomes a no-op.
    """
    definitions = SqlAlchemyToolDefinitionPersistence(engine)
    reconcile_builtin_definitions(definitions, specs=builtin_tool_specs(clock=lambda: now), now=now)
    return tuple(
        persisted.to_descriptor()
        for persisted in definitions.list_for_source(source_ref=BUILTIN_SOURCE_REF)
    )


def definition_status(engine: Engine, tool_definition_id: int) -> str:
    """Read one definition's durable status, which is what ``unsupported_schema`` asserts."""
    with engine.connect() as connection:
        return str(
            connection.execute(
                text("SELECT status FROM tool_definitions WHERE id = :i"),
                {"i": tool_definition_id},
            ).scalar_one()
        )


def reviewed_fingerprint(engine: Engine, *, instance_id: int, tool_definition_id: int) -> str:
    """The exact fingerprint bytes the existing grant reviewed, read straight from the grant row.

    This is deliberately not the same value as the definition's *current* fingerprint: a drift
    journey asserts the grant still reviews the old bytes while the definition has moved on.
    """
    with engine.connect() as connection:
        return str(
            connection.execute(
                select(AgentToolGrantRecord.reviewed_fingerprint).where(
                    AgentToolGrantRecord.agent_instance_id == instance_id,
                    AgentToolGrantRecord.tool_definition_id == tool_definition_id,
                )
            ).scalar_one()
        )


def job_and_attempt_counts(engine: Engine, run_id: int) -> tuple[int, int, int]:
    """Return (runs, jobs, attempts) for one Run, which must stay (1, 1, 1) for a whole loop."""
    with engine.connect() as connection:
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


async def add_stdio_connection(
    engine: Engine,
    *,
    server_key: str,
    config: McpOperatorConfig,
    policy: EgressPolicy,
    display_name: str = "Fake Stdio Docs",
    owner_user_id: int = OWNER_USER_ID,
    now: datetime = NOW,
) -> tuple[int, tuple[ToolDescriptor, ...]]:
    """Create one durable stdio connection and immediately discover its catalog for real.

    The row carries a ``server_key`` and no endpoint, so the gateway resolves the operator-declared
    process from configuration at dispatch -- which is what makes ``server_key`` the thing under
    test rather than a URL.
    """
    connections = SqlAlchemyMcpConnectionPersistence(engine)
    connection_id = connections.create(
        owner_user_id=owner_user_id,
        display_name=display_name,
        transport=ConnectionTransport.STDIO,
        endpoint=None,
        server_key=server_key,
        credential_ref=None,
        now=now,
    )
    descriptors = await discover_connection(
        engine,
        connection_id=connection_id,
        config=config,
        policy=policy,
        owner_user_id=owner_user_id,
        now=now,
    )
    return connection_id, descriptors


def subprocess_available() -> bool:
    """Whether this platform can start a Python child process at all.

    The stdio journey is genuinely platform-gated: without a working ``subprocess`` there is no
    child to prove started and none to prove terminated, so its test is skipped rather than
    weakened.
    """
    try:
        completed = subprocess.run(
            [sys.executable, "-c", "pass"],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except OSError:
        return False
    return completed.returncode == 0


def record_stdio_processes(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Capture the child process the SDK spawns for a stdio connection.

    The SDK keeps its process handle private, and the only dependency-free way to observe the
    *real* child is to wrap the SDK's own platform spawn function. The wrapper adds no behaviour: it
    records the object the SDK created and returns it unchanged, so teardown semantics stay the
    SDK's.
    """
    import mcp.client.stdio as stdio_module

    spawned: list[Any] = []
    original = stdio_module._create_platform_compatible_process  # pyright: ignore[reportPrivateUsage]

    async def recording(*args: Any, **kwargs: Any) -> Any:
        process = await original(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(
        stdio_module, "_create_platform_compatible_process", recording, raising=True
    )
    return spawned


__all__ = [
    "PROVIDER_ID",
    "McpStageD",
    "add_stdio_connection",
    "build_execution",
    "builtin_descriptors",
    "claim_only",
    "definition_status",
    "execute_run",
    "job_and_attempt_counts",
    "record_stdio_processes",
    "reviewed_fingerprint",
    "run_events",
    "subprocess_available",
    "tool_events",
]
