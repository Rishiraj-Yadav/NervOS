"""D7 Worker-composed Stage-D acceptance: lifecycle, concurrency, fairness, and fencing.

Every journey here runs through `JobExecutionService` + `RunExecutor` with a real `tool_loop`
wired in, because that routing seam -- a Run's own frozen limits deciding the tool shape -- is
exactly what no pre-D7 test crossed. The Worker rig is extended (never replaced) with the same
`tool_loop` argument production's composition root passes, so a Run that a control plane admitted
as tool-enabled takes the loop, and one that was admitted tool-free keeps the Stage B/C path.

The journeys are deliberately about what the engine *commits*:

* J-S holds a multi-turn tool Attempt open and reads the live-claim counts the frozen C6 policy
  bounds, so "one Attempt occupies one slot for the whole loop" is observed rather than assumed.
* J-T carries tool-enabled work through the fairness topology: long tool work must not create a
  duplicate claim, a continuation Job, or a second chance to jump the queue.
* J-V loses the claim mid-dispatch and lets the remote result arrive *late*; the stale Worker's
  success write must be fenced out entirely while authoritative recovery owns the outcome.
* J-W drives the production `close_worker` consumer over the `create_worker` composition, proving
  the MCP lifecycle owner a child Attempt created is closed by the Worker's own shutdown task.
* J-Y boots a Worker against a freshly migrated database and proves the schema gate, composition,
  and shutdown leave nothing behind.

Time is a parameter, never a wait: leases expire by passing a later `now`, and no journey sleeps to
observe a backoff.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from nervos_core.application.builtin_tools import (
    BUILTIN_SOURCE_REF,
    BuiltinToolSource,
    create_builtin_tool_registry,
)
from nervos_core.application.job_execution import LEASE_DURATION
from nervos_core.application.mcp_connections import ConnectionTransport
from nervos_core.application.model_completion import (
    ModelCompletion,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    StopOutcome,
    ToolCall,
)
from nervos_core.application.queue_policy import (
    GLOBAL_ACTIVE_LIMIT,
    MAX_ACTIVE_LIMIT,
    PER_AGENT_ACTIVE_LIMIT,
    PER_PROVIDER_ACTIVE_LIMIT,
    QueuePolicy,
)
from nervos_core.application.run_execution import ToolLoopHandler
from nervos_core.application.tool_loop import ToolLoop
from nervos_core.application.tool_registry import ToolDescriptor, ToolRegistry, ToolResult
from nervos_core.application.trusted_chat import NERVOS_TOOL_CHAT_SYSTEM_INSTRUCTION
from nervos_core.domain.runs import TOOL_ENABLED_LIMITS, RunStatus
from nervos_core.domain.tools import JsonValue
from nervos_core.infrastructure.database import create_sqlite_engine
from nervos_core.infrastructure.database.jobs import SqlAlchemyJobExecutionPersistence
from nervos_core.infrastructure.database.mcp_connections import (
    SqlAlchemyMcpConnectionPersistence,
)
from nervos_core.infrastructure.database.schema_revision import (
    read_schema_revision,
    require_schema_revision,
)
from nervos_core.infrastructure.database.tool_definitions import (
    SqlAlchemyToolDefinitionPersistence,
)
from nervos_core.infrastructure.database.tool_invocations import (
    SqlAlchemyToolInvocationPersistence,
)
from nervos_core.infrastructure.database.tools import SqlAlchemyToolPermissionEvaluator
from nervos_mcp import gateway as gateway_module
from nervos_worker.app import (
    EXPECTED_SCHEMA_REVISION,
    close_worker,
    create_worker,
    reconcile_tool_definitions,
)
from nervos_worker.config import WorkerSettings
from nervos_worker.main import run_worker
from sqlalchemy import Engine, text
from support import (
    NOW,
    PROVIDER_ID,
    RecordingCompletion,
    build_execution_service,
    build_worker,
    migrate,
    reclaim_expired_claim,
    run_until_all_jobs_terminal,
    submit,
)

TOOL_NAME = "current_time"
GRANTED_ARGUMENTS = '{"timezone":"UTC"}'
ROOT = Path(__file__).resolve().parents[4]

# `JobExecutionService.execute` performs the start itself, so a test must claim without starting.
WORKER_ID = "lifecycle-worker"


async def _no_wait(_seconds: float) -> None:
    """A sleeper that returns at once, so a retry ladder costs no wall clock."""


# --------------------------------------------------------------------------------------------
# The tool-enabled composition, assembled the way the Worker's composition root assembles it.
# --------------------------------------------------------------------------------------------


def _tool_loop(
    engine: Engine,
    *,
    registry: ToolRegistry | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ToolLoop:
    """Build the real D4 loop over durable persistence, with a fixed clock and no waiting."""
    now = clock or (lambda: NOW)
    return ToolLoop(
        registry=registry
        or create_builtin_tool_registry(SqlAlchemyToolDefinitionPersistence(engine), clock=now),
        source_ref=BUILTIN_SOURCE_REF,
        authorize=SqlAlchemyToolPermissionEvaluator(engine),
        invocations=SqlAlchemyToolInvocationPersistence(engine),
        usage=SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None),
        system_instruction=NERVOS_TOOL_CHAT_SYSTEM_INSTRUCTION,
        clock=now,
        sleep=_no_wait,
    )


def _grant_current_time(engine: Engine, *, agents: tuple[int, ...] = (1,)) -> ToolDescriptor:
    """Make the built-in definitions durable and grant one to each named Agent Instance.

    Reconciliation is the production helper the Worker runs at startup, and the grant is written
    before submission because a Run snapshots its grant cutoff at submission -- a grant created
    afterwards must not reach it.
    """
    descriptor = next(
        item for item in reconcile_tool_definitions(engine) if item.upstream_name == TOOL_NAME
    )
    with engine.begin() as connection:
        for agent_instance_id in agents:
            connection.execute(
                text(
                    "INSERT INTO agent_tool_grants"
                    " (agent_instance_id, tool_definition_id, reviewed_fingerprint, created_at)"
                    " VALUES (:a, :d, :f, :n)"
                ),
                {
                    "a": agent_instance_id,
                    "d": descriptor.tool_definition_id,
                    "f": descriptor.fingerprint,
                    "n": NOW,
                },
            )
    return descriptor


class ToolScriptedCompletion(RecordingCompletion):
    """A `RecordingCompletion` whose first turn per Run asks for one granted tool.

    Keyed by `user_text` because a `ModelRequest` carries no Run identity and the loop keeps the
    Run's input text constant across turns. The second turn is the ordinary final answer, so the
    loop is genuinely multi-turn while the provider double stays a provider double.
    """

    def __init__(self, *, model_name: str, reply: str = "the tool loop finished") -> None:
        super().__init__(provider_id=PROVIDER_ID, reply=reply)
        self._model_name = model_name
        self._turns: dict[str, int] = {}
        # The base double's own `calls` counter is inherited; this one is annotated here so a
        # journey can assert the model-turn count without reaching into the shared rig.
        self.model_calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.model_calls += 1
        self.requests.append(request)
        turn = self._turns.get(request.user_text, 0)
        self._turns[request.user_text] = turn + 1
        if turn == 0:
            return ModelResponse(
                "",
                self.provider_id,
                request.model_name,
                StopOutcome.TOOL_USE,
                ModelUsage(1, 1, 2),
                (
                    ToolCall(
                        call_id=f"call-{request.user_text}",
                        name=self._model_name,
                        arguments_json=GRANTED_ARGUMENTS,
                    ),
                ),
            )
        return ModelResponse(
            self.reply,
            self.provider_id,
            request.model_name,
            StopOutcome.STOP,
            ModelUsage(11, 7, 18),
        )


def _live_claims(database_path: Path) -> tuple[int, dict[int, int], dict[str, int]]:
    """Count live claims globally, per Agent Instance, and per provider, straight from the jobs.

    Read through a separate engine over the same file, so the measurement is the database's view
    of concurrency rather than a counter any Worker maintains.
    """
    engine = create_sqlite_engine(database_path)
    try:
        with engine.connect() as connection:
            agent_rows = connection.execute(
                text(
                    "SELECT agent_instance_id, count(*) FROM jobs"
                    " WHERE status IN ('claimed','running') GROUP BY agent_instance_id"
                )
            ).all()
            provider_rows = connection.execute(
                text(
                    "SELECT model_provider, count(*) FROM jobs"
                    " WHERE status IN ('claimed','running') GROUP BY model_provider"
                )
            ).all()
    finally:
        engine.dispose()
    per_agent = {int(row[0]): int(row[1]) for row in agent_rows}
    per_provider = {str(row[0]): int(row[1]) for row in provider_rows}
    return sum(per_agent.values()), per_agent, per_provider


async def _poll(predicate: Callable[[], bool], *, attempts: int = 500) -> bool:
    """Wait, without a real backoff, until the predicate holds or the bounded attempts run out."""
    for _ in range(attempts):
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def _invocation_rows(engine: Engine, run_id: int) -> list[tuple[str, str | None]]:
    with engine.connect() as connection:
        return [
            (str(status), None if code is None else str(code))
            for status, code in connection.execute(
                text(
                    "SELECT status, error_code FROM tool_invocations"
                    " WHERE run_id = :r ORDER BY tool_sequence"
                ),
                {"r": run_id},
            ).all()
        ]


def _run_shape(engine: Engine, run_id: int) -> tuple[int, int, int]:
    with engine.connect() as connection:
        runs = int(
            connection.scalar(text("SELECT count(*) FROM runs WHERE id = :r"), {"r": run_id}) or 0
        )
        jobs = int(
            connection.scalar(text("SELECT count(*) FROM jobs WHERE run_id = :r"), {"r": run_id})
            or 0
        )
        attempts = int(
            connection.scalar(
                text(
                    "SELECT count(*) FROM job_attempts a JOIN jobs j ON j.id = a.job_id"
                    " WHERE j.run_id = :r"
                ),
                {"r": run_id},
            )
            or 0
        )
    return runs, jobs, attempts


def _run_status(engine: Engine, run_id: int) -> tuple[str, str | None]:
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT status, error_code FROM runs WHERE id = :r"), {"r": run_id}
        ).one()
    return str(row[0]), None if row[1] is None else str(row[1])


def _event_types(engine: Engine, run_id: int) -> list[str]:
    with engine.connect() as connection:
        return [
            str(value)
            for value in connection.execute(
                text("SELECT event_type FROM run_events WHERE run_id = :r ORDER BY sequence"),
                {"r": run_id},
            ).scalars()
        ]


# --------------------------------------------------------------------------------------------
# J-S: a multi-turn tool Attempt obeys every frozen C6 concurrency dimension.
# --------------------------------------------------------------------------------------------


class HoldingToolCompletion(ToolScriptedCompletion):
    """A tool completion that parks its first turn until released, recording live claims.

    The recorded counts are taken from inside the provider call -- the one moment a claim is
    definitely live -- so a cap exceeded during the overlap is visible here rather than inferred
    from a final state.
    """

    def __init__(self, *, model_name: str, database_path: Path, hold: asyncio.Event) -> None:
        super().__init__(model_name=model_name)
        self._database_path = database_path
        self._hold = hold
        self.max_live_global = 0
        self.max_live_per_agent = 0
        self.max_live_per_provider = 0
        self.observed_live: list[tuple[int, int, int]] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        live = await asyncio.to_thread(_live_claims, self._database_path)
        self.observed_live.append(
            (live[0], max(live[1].values(), default=0), max(live[2].values(), default=0))
        )
        self.max_live_global = max(self.max_live_global, live[0])
        self.max_live_per_agent = max(self.max_live_per_agent, max(live[1].values(), default=0))
        self.max_live_per_provider = max(
            self.max_live_per_provider, max(live[2].values(), default=0)
        )
        if self._turns.get(request.user_text, 0) == 0:
            # Hold the claim open across the slow, multi-turn part of the loop.
            await self._hold.wait()
        return await super().complete(request)


@pytest.mark.anyio
async def test_a_multi_turn_tool_attempt_holds_one_slot_and_never_exceeds_the_frozen_caps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The frozen C6 limits bind a tool-enabled fleet exactly as they bind a tool-free one.

    Each Run calls the provider twice (a tool turn and a final turn) under one live claim, so a
    loop that released and re-took its slot between turns would push the observed count above the
    authoritative limit. The counts are read at the frozen production values, not a relaxed policy.
    """
    database = tmp_path / "j-s.db"
    engine = migrate(database, monkeypatch, agents=1)
    try:
        descriptor = _grant_current_time(engine)
        extra = 2
        run_ids = [
            submit(engine, text_value=f"hold-{index}", limits=TOOL_ENABLED_LIMITS)
            for index in range(GLOBAL_ACTIVE_LIMIT + extra)
        ]

        hold = asyncio.Event()
        completion: HoldingToolCompletion = HoldingToolCompletion(
            model_name=descriptor.model_name, database_path=database, hold=hold
        )
        completions: dict[str, ModelCompletion] = {PROVIDER_ID: completion}
        policy = QueuePolicy(
            global_active_limit=GLOBAL_ACTIVE_LIMIT,
            per_agent_active_limit=PER_AGENT_ACTIVE_LIMIT,
            per_provider_active_limit=PER_PROVIDER_ACTIVE_LIMIT,
        )
        # The policy is a ceiling the Worker never raises: it is handed a budget far above the
        # authoritative one so the frozen constants, not the Worker's slots, are what binds.
        worker = _tool_worker(
            engine,
            completions,
            policy=policy,
            concurrency=GLOBAL_ACTIVE_LIMIT + extra,
            max_active=GLOBAL_ACTIVE_LIMIT + extra,
        )
        task = asyncio.create_task(run_until_all_jobs_terminal(worker, engine))
        try:
            # Every claim that can be live is now parked inside its first model call.
            saturated = await _poll(lambda: completion.max_live_global == GLOBAL_ACTIVE_LIMIT)
            assert saturated, f"the cap was never reached: {completion.observed_live}"
            live_now, per_agent, per_provider = _live_claims(database)
            assert live_now == GLOBAL_ACTIVE_LIMIT
            assert max(per_agent.values(), default=0) <= PER_AGENT_ACTIVE_LIMIT
            assert max(per_provider.values(), default=0) <= PER_PROVIDER_ACTIVE_LIMIT

            hold.set()
            await asyncio.wait_for(task, timeout=60)
        finally:
            hold.set()
            if not task.done():
                task.cancel()

        # No observation ever saw a count above the frozen dimension it belongs to.
        for global_live, agent_live, provider_live in completion.observed_live:
            assert global_live <= GLOBAL_ACTIVE_LIMIT
            assert agent_live <= PER_AGENT_ACTIVE_LIMIT
            assert provider_live <= PER_PROVIDER_ACTIVE_LIMIT
        assert completion.max_live_global == GLOBAL_ACTIVE_LIMIT
        # A sanity bound on the frozen constants: no dimension may exceed MAX_ACTIVE_LIMIT.
        assert (
            max(GLOBAL_ACTIVE_LIMIT, PER_AGENT_ACTIVE_LIMIT, PER_PROVIDER_ACTIVE_LIMIT)
            <= MAX_ACTIVE_LIMIT
        )

        assert [_run_status(engine, run_id)[0] for run_id in run_ids] == [
            RunStatus.SUCCEEDED.value
        ] * len(run_ids)
        # Two model turns each, still one Run, one Job, one Attempt, one invocation -- the loop
        # never minted a continuation Job to carry the second turn.
        for run_id in run_ids:
            assert _run_shape(engine, run_id) == (1, 1, 1)
            assert _invocation_rows(engine, run_id) == [("succeeded", None)]
        assert completion.model_calls == 2 * len(run_ids)
    finally:
        engine.dispose()


# --------------------------------------------------------------------------------------------
# J-T: tool-enabled work keeps the fairness ordering and mints no duplicate obligation.
# --------------------------------------------------------------------------------------------


class ToolOrderingCompletion(ToolScriptedCompletion):
    """A tool completion that records the order in which Runs' *first* turns were served."""

    def __init__(self, *, model_name: str) -> None:
        super().__init__(model_name=model_name)
        self.first_calls: list[str] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if self._turns.get(request.user_text, 0) == 0:
            self.first_calls.append(request.user_text)
        return await super().complete(request)


@pytest.mark.anyio
async def test_tool_enabled_work_preserves_fairness_and_mints_no_continuation_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long tool loop is served like any other Run: no jump, no duplicate claim, no extra Job.

    Agent 1 submits six tool-enabled Runs before Agent 2 submits two, and the global budget is one.
    Least-recently-served fairness must still interleave the newcomer, and because the loop holds
    its single claim for both turns, the ordering is the Run-level ordering rather than a turn-level
    one.
    """
    engine = migrate(tmp_path / "j-t.db", monkeypatch, agents=2)
    try:
        descriptor = _grant_current_time(engine, agents=(1, 2))
        agent_one = [
            submit(
                engine, text_value=f"a1-{index}", agent_instance_id=1, limits=TOOL_ENABLED_LIMITS
            )
            for index in range(6)
        ]
        agent_two = [
            submit(
                engine, text_value=f"a2-{index}", agent_instance_id=2, limits=TOOL_ENABLED_LIMITS
            )
            for index in range(2)
        ]

        completion = ToolOrderingCompletion(model_name=descriptor.model_name)
        completions: dict[str, ModelCompletion] = {PROVIDER_ID: completion}
        worker = _tool_worker(
            engine,
            completions,
            policy=QueuePolicy(global_active_limit=1),
            concurrency=1,
            max_active=1,
        )

        await run_until_all_jobs_terminal(worker, engine)

        # The newcomer was served twice while Agent 1 still had four Runs outstanding.
        assert completion.first_calls[:4] == ["a1-0", "a2-0", "a1-1", "a2-1"]
        assert sorted(completion.first_calls) == sorted(
            [f"a1-{index}" for index in range(6)] + [f"a2-{index}" for index in range(2)]
        )
        assert [_run_status(engine, run_id)[0] for run_id in agent_one + agent_two] == [
            RunStatus.SUCCEEDED.value
        ] * 8
        # One Run, one Job, one Attempt per Run: the second turn added no obligation of its own,
        # and no Attempt ever ran twice (no duplicate claim).
        for run_id in agent_one + agent_two:
            assert _run_shape(engine, run_id) == (1, 1, 1)
            assert _invocation_rows(engine, run_id) == [("succeeded", None)]
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT max(attempt_count) FROM jobs")) == 1
    finally:
        engine.dispose()


# --------------------------------------------------------------------------------------------
# J-V: a late remote result cannot resurrect a claim that real reclamation already fenced.
# --------------------------------------------------------------------------------------------


class DeferredToolExecutor:
    """A tool executor whose result arrives only when the test releases it.

    This models the D6 MCP-dispatch shape the D6 fencing suite did not: a call whose `started` row
    is committed, whose claim is then reclaimed by the real engine, and whose answer arrives
    afterwards from the remote side. The delay is a control on the test, never a real wait.
    """

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.dispatched = 0

    async def execute(
        self, descriptor: ToolDescriptor, arguments: Mapping[str, JsonValue]
    ) -> ToolResult:
        del descriptor, arguments
        self.dispatched += 1
        await self.release.wait()
        return ToolResult(text="late remote result")


def _deferred_registry(engine: Engine, executor: DeferredToolExecutor) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        source_ref=BUILTIN_SOURCE_REF,
        source=BuiltinToolSource(SqlAlchemyToolDefinitionPersistence(engine)),
        executor=executor,
    )
    return registry


def _invocation_status(engine: Engine, run_id: int) -> str | None:
    rows = _invocation_rows(engine, run_id)
    return None if not rows else rows[0][0]


@pytest.mark.anyio
async def test_a_late_remote_result_is_fenced_after_real_reclamation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old Worker's write after a lost lease is fenced; recovery owns the outcome.

    The existing D6 proof (`test_a_worker_that_lost_its_claim_cannot_append_a_tool_event`) fences a
    pre-dispatch refusal. This journey adds the ordering D7 exists for: the claim is lost while a
    call is genuinely in flight, and the *late successful result* then returns to the stale Worker.
    Its success write must be fenced out, so there is no observation, no next model turn, and no
    resurrection -- while the authoritative recovery closes the invocation `started -> ambiguous`
    without replaying it.
    """
    engine = migrate(tmp_path / "j-v.db", monkeypatch, agents=1)
    try:
        descriptor = _grant_current_time(engine)
        run_id = submit(engine, text_value="late", limits=TOOL_ENABLED_LIMITS)

        executor = DeferredToolExecutor()
        loop = _tool_loop(engine, registry=_deferred_registry(engine, executor))
        completion = ToolScriptedCompletion(model_name=descriptor.model_name)
        persistence, service = build_execution_service(
            engine, {PROVIDER_ID: completion}, tool_loop=loop
        )
        claim = persistence.claim_next(
            worker_id=WORKER_ID,
            provider_ids=(PROVIDER_ID,),
            max_active=4,
            now=NOW,
            lease_duration=LEASE_DURATION,
        )
        assert claim is not None and claim.run_id == run_id

        task = asyncio.create_task(service.execute(claim))
        try:
            # The durable boundary: `started` is committed, so the call may already have landed.
            started = await _poll(lambda: _invocation_status(engine, run_id) == "started")
            assert started, "the invocation never reached the started boundary"

            # Real C3 reclamation, at an instant past the lease, fences the in-flight Attempt.
            reclaimed = reclaim_expired_claim(
                engine, now=NOW + LEASE_DURATION + timedelta(seconds=1)
            )
            assert reclaimed is not None and reclaimed.run_id == run_id
            # Authoritative recovery closes the started call ambiguous, with no replay.
            assert _invocation_rows(engine, run_id) == [("ambiguous", "tool_outcome_unknown")]
            assert _run_status(engine, run_id) == ("failed", "execution_outcome_ambiguous")
            events_at_recovery = _event_types(engine, run_id)

            # The remote answer now returns to the Worker that no longer owns the claim.
            executor.release.set()
            await asyncio.wait_for(task, timeout=30)
        finally:
            executor.release.set()
            if not task.done():
                task.cancel()

        # Fenced: the success write changed nothing, so no success event and no replay appeared.
        assert "tool.succeeded" not in _event_types(engine, run_id)
        assert _event_types(engine, run_id) == events_at_recovery
        assert _invocation_rows(engine, run_id) == [("ambiguous", "tool_outcome_unknown")]
        assert _run_shape(engine, run_id) == (1, 1, 1)
        # No observation was fed back, so the model was never consulted a second time.
        assert executor.dispatched == 1
        assert completion.model_calls == 1
        # The Run was not resurrected by the stale Worker.
        assert _run_status(engine, run_id) == ("failed", "execution_outcome_ambiguous")
    finally:
        engine.dispose()


# --------------------------------------------------------------------------------------------
# J-W: the production shutdown consumer closes a session a child Attempt created.
# --------------------------------------------------------------------------------------------


def _count_stdio_transport_opens(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every stdio transport the gateway builds -- exactly one per live client.

    `_open_client` calls the module-level factory once per client, so the recorded list is the
    number of sessions the gateway really opened, without reading its private cache. The stdio
    transport is the production shape that needs no egress decision, which is why the composition
    root and its strict egress policy stay exactly as production built them.
    """
    opened: list[str] = []
    real = gateway_module.stdio_transport

    def counting(spec: Any, *, credential: Any = None) -> Any:
        opened.append(str(spec.server_key))
        return real(spec, credential=credential)

    monkeypatch.setattr(gateway_module, "stdio_transport", counting)
    return opened


def _lifecycle_owners() -> list[asyncio.Task[Any]]:
    """The gateway's per-connection lifecycle tasks, identified by the coroutine they run."""
    return [
        task
        for task in asyncio.all_tasks()
        if "_own_session" in (getattr(task.get_coro(), "__qualname__", "") or "")
    ]


def _stdio_operator_json() -> str:
    """Declare the existing fake stdio server as an operator-owned process.

    It is the same fake the D5 integration suite uses, launched as a real child process exactly as
    the stdio transport launches any server; only the interpreter path and the module root are
    supplied, so no fake is re-implemented here.
    """
    module_root = ROOT / "packages" / "nervos-mcp"
    script = (
        f"import sys; sys.path.insert(0, r'{module_root}'); "
        "from tests.support.fake_stdio_server import main; main()"
    )
    return json.dumps({"fake": {"executable": sys.executable, "args": ["-c", script]}})


@pytest.mark.anyio
async def test_close_worker_closes_a_session_created_by_a_child_attempt_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`close_worker` over the real `create_worker` composition shuts MCP sessions down cleanly.

    An anyio cancel scope belongs to the task that entered it, so a session created during an
    Attempt -- in its own task -- can only be closed by asking its lifecycle owner to exit. These
    are the gateway's own guarantees, but here they are exercised through the production consumer
    the Worker's `finally` actually calls, over the composition root production actually builds.
    """
    database = tmp_path / "j-w.db"
    engine = migrate(database, monkeypatch, agents=1)
    try:
        settings = WorkerSettings(
            database_path=database,
            environment="test",
            mcp_stdio_servers=_stdio_operator_json(),
        )
        composition = create_worker(settings)
        try:
            connection_id = SqlAlchemyMcpConnectionPersistence(engine).create(
                owner_user_id=1,
                display_name="fake",
                transport=ConnectionTransport.STDIO,
                endpoint=None,
                server_key="fake",
                credential_ref=None,
                now=NOW,
            )
            opened = _count_stdio_transport_opens(monkeypatch)
            assert _lifecycle_owners() == []

            # Cold, concurrent first-callers share one creation, so exactly one session exists.
            calls = [f"cold-{index}.txt" for index in range(5)]
            results = await asyncio.gather(
                *(
                    asyncio.create_task(
                        composition.mcp_gateway.call_tool(
                            connection_id, "read_document", {"path": path}
                        )
                    )
                    for path in calls
                ),
                return_exceptions=True,
            )
            for path, result in zip(calls, results, strict=True):
                assert not isinstance(result, BaseException), result
                assert result.is_error is False
                assert result.content[0].text == f"document:{path}"
            assert opened == ["fake"], "a cold burst built more than one session"

            # A warm caller in a different child task reuses the same session.
            warm = await asyncio.create_task(
                composition.mcp_gateway.call_tool(
                    connection_id, "read_document", {"path": "warm.txt"}
                )
            )
            assert warm.content[0].text == "document:warm.txt"
            assert opened == ["fake"]
            assert len(_lifecycle_owners()) == 1

            # The Worker's own shutdown task now closes the Attempt-created session.
            await close_worker(composition)
            assert _lifecycle_owners() == [], "close_worker left a lifecycle owner running"
        finally:
            await composition.mcp_gateway.close_all()

        # ... and the same shutdown path is safe to run a second time.
        await composition.mcp_gateway.close_all()
        assert _lifecycle_owners() == []
    finally:
        engine.dispose()


# --------------------------------------------------------------------------------------------
# J-Y: a fresh database, a real boot, and a shutdown that leaks nothing.
# --------------------------------------------------------------------------------------------


def _worker_rows(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(connection.scalar(text("SELECT count(*) FROM workers")) or 0)


@pytest.mark.anyio
async def test_a_fresh_database_boots_through_the_schema_gate_and_shuts_down_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alembic head -> the Worker's revision gate -> a composed, ready Worker -> clean shutdown.

    The readiness marker is written only after the schema gate, tool reconciliation, MCP source
    synchronisation, registry registration, and the startup reclamation pass, so observing the file
    is a deterministic proof of a fully-initialised Worker without a sleep. Only the OS-signal
    plumbing is replaced, because it is the one part of the boot path a test process cannot exercise
    portably; the captured stop event drives the same loop.

    Note, honestly: the API has no schema-revision gate at all -- only the Worker refuses to boot
    against a wrong revision, and only that claim is made here.
    """
    database = (tmp_path / "fresh.db").resolve()
    engine = migrate(database, monkeypatch, agents=0)
    marker = tmp_path / "run" / "worker.ready"
    settings = WorkerSettings(
        database_path=database,
        environment="test",
        worker_ready_file=marker,
    )
    try:
        assert read_schema_revision(engine) == EXPECTED_SCHEMA_REVISION
        assert require_schema_revision(engine, EXPECTED_SCHEMA_REVISION) == EXPECTED_SCHEMA_REVISION
        assert _worker_rows(engine) == 0

        captured: list[asyncio.Event] = []

        def capture(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> list[Any]:
            del loop
            captured.append(stop)
            return []

        # `nervos_worker/__init__` re-exports `main` as a function, so the submodule is addressed
        # through `sys.modules` rather than by attribute.
        monkeypatch.setattr(sys.modules["nervos_worker.main"], "install_stop_handlers", capture)

        before = set(asyncio.all_tasks())
        task = asyncio.create_task(run_worker(settings))
        try:
            ready = await _poll(marker.exists)
            assert ready, "the Worker never wrote its readiness marker"
            contents = marker.read_text(encoding="utf-8")
            assert f"schema_revision={EXPECTED_SCHEMA_REVISION}" in contents
            # The composed Worker registered itself durably, which happens after the MCP gateway
            # was built by the composition root.
            assert _worker_rows(engine) == 1
            assert len(captured) == 1

            captured[0].set()
            assert await asyncio.wait_for(task, timeout=30) == 0
        finally:
            if not task.done():
                task.cancel()

        assert set(asyncio.all_tasks()) - before == set(), "the Worker leaked a task"
    finally:
        engine.dispose()


# --------------------------------------------------------------------------------------------
# The rig extension: the same Worker composition, with the tool loop production wires.
# --------------------------------------------------------------------------------------------


def _tool_worker(
    engine: Engine,
    completions: dict[str, ModelCompletion],
    *,
    policy: QueuePolicy,
    concurrency: int,
    max_active: int,
) -> Any:
    """Compose the shipped Worker over a tool-enabled execution service.

    This is the `support.build_worker` composition with the one argument production always passes
    and no pre-D7 Worker test did: the real `tool_loop`. Keeping it next to the journeys makes the
    routing seam explicit rather than hiding it behind a default.
    """
    loop: ToolLoopHandler = _tool_loop(engine)
    return build_worker(
        engine,
        completions,
        worker_id=WORKER_ID,
        concurrency=concurrency,
        max_active=max_active,
        poll_interval=0.01,
        policy=policy,
        tool_loop=loop,
    )
