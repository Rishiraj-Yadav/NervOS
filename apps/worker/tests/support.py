"""Shared fixtures for the C3 Worker tests."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from nervos_core.application.job_execution import JobExecutionService
from nervos_core.application.lease_reclamation import LeaseReclaimer, ReclaimedClaim
from nervos_core.application.model_completion import (
    ModelCompletion,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    StopOutcome,
)
from nervos_core.application.queue_policy import PRODUCTION_QUEUE_POLICY, QueuePolicy
from nervos_core.application.run_execution import RunExecutor, ToolLoopHandler
from nervos_core.application.trusted_chat import create_builtin_handler_registry
from nervos_core.domain.runs import STAGE_B_LIMITS, RunLimits
from nervos_core.infrastructure.database import create_sqlite_engine
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobExecutionPersistence,
    SqlAlchemyJobPersistence,
)
from nervos_worker.registry import ReclaimLoop, WorkerRegistry
from nervos_worker.service import Worker
from sqlalchemy import Engine, text

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 9, 14, tzinfo=UTC)
PROVIDER_ID = "anthropic"
SECOND_PROVIDER_ID = "openai"


def migrate(
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    agents: int = 1,
    providers: tuple[str, ...] = (PROVIDER_ID,),
) -> Engine:
    """Create one disposable migrated database holding a user and `agents` Agent Instances.

    Providers cycle over `providers`, so a suite can build a fleet whose Instances map onto
    distinct model providers. The default keeps the single anthropic Chat Instance that the
    earlier Worker suites assume, at id 1.
    """
    monkeypatch.setenv("NERVOS_DATABASE_PATH", str(path))
    command.upgrade(Config(str(ROOT / "apps" / "api" / "alembic.ini")), "head")
    engine = create_sqlite_engine(path)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users"
                "(username,password_hash,role,is_active,created_at,updated_at) "
                "VALUES('owner',X'00','admin',1,:n,:n)"
            ),
            {"n": NOW},
        )
        for index in range(1, agents + 1):
            provider = providers[(index - 1) % len(providers)]
            connection.execute(
                text(
                    "INSERT INTO agent_instances"
                    "(owner_user_id,agent_key,agent_definition_version,display_name,enabled,"
                    "model_provider,model_name,created_at,updated_at) "
                    "VALUES(1,'nervos.chat','1',:d,1,:p,:m,:n,:n)"
                ),
                {"d": f"Agent {index}", "p": provider, "m": "opaque/model", "n": NOW},
            )
    return engine


def submit(
    engine: Engine,
    *,
    text_value: str = "hello",
    agent_instance_id: int = 1,
    limits: RunLimits = STAGE_B_LIMITS,
) -> int:
    """Durably accept one Run and return its identifier.

    `limits` defaults to the Stage B/C snapshot, so a caller that wants the tool-enabled shape must
    name `TOOL_ENABLED_LIMITS` explicitly -- the routing decision the D7 acceptance suites exercise
    is read from the Run's own frozen limits, never inferred from the registry.
    """
    run = SqlAlchemyJobPersistence(engine, max_pending=1000).submit(
        owner_user_id=1,
        agent_instance_id=agent_instance_id,
        input_text=text_value,
        limits=limits,
        now=NOW,
    )
    return run.id


def counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            table: int(connection.scalar(text(f"SELECT count(*) FROM {table}")) or 0)
            for table in ("runs", "jobs", "job_attempts", "run_events")
        }


def run_row(engine: Engine, run_id: int) -> dict[str, object]:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text(
                    "SELECT status, output_text, finish_reason, error_code, error_message,"
                    " elapsed_ms, started_at FROM runs WHERE id=:r"
                ),
                {"r": run_id},
            )
            .mappings()
            .one()
        )


class MutableClock:
    """An injectable clock so a durable due instant can be reached without sleeping.

    C4's retry wait is an absolute instant on disk, so proving that a later Worker claims it
    requires advancing time, not waiting out the backoff.
    """

    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value = self.value + timedelta(seconds=seconds)


def job_row(engine: Engine, run_id: int) -> dict[str, object]:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text(
                    "SELECT id, status, attempt_count, claimed_by, claim_token, error_code,"
                    " available_at, cancel_requested_at FROM jobs WHERE run_id=:r"
                ),
                {"r": run_id},
            )
            .mappings()
            .one()
        )


def attempt_rows(engine: Engine) -> list[dict[str, object]]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT attempt_number, status, retry_disposition, error_code"
                    " FROM job_attempts ORDER BY id"
                )
            )
            .mappings()
            .all()
        ]


def event_types(engine: Engine, run_id: int) -> list[str]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                text("SELECT event_type FROM run_events WHERE run_id=:r ORDER BY sequence"),
                {"r": run_id},
            ).scalars()
        )


def worker_rows(engine: Engine) -> list[dict[str, object]]:
    """Every registry row, ordered by insertion, as the C3 tests need to inspect them."""
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT worker_id, started_at, last_heartbeat_at, stopped_at"
                    " FROM workers ORDER BY id"
                )
            )
            .mappings()
            .all()
        ]


def reclaim_expired_claim(engine: Engine, *, now: datetime) -> ReclaimedClaim | None:
    """Force one reclamation pass at an explicit instant, bypassing the Worker loop."""
    persistence = SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None)
    return persistence.reclaim_next_expired_claim(now=now, backoff=timedelta(seconds=5))


class RecordingCompletion:
    """Deterministic offline completion double recording every provider call."""

    def __init__(self, provider_id: str = PROVIDER_ID, reply: str = "worker answer") -> None:
        self.provider_id = provider_id
        self.reply = reply
        self.calls = 0
        self.requests: list[ModelRequest] = []
        self.error: Exception | None = None

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return ModelResponse(
            self.reply,
            self.provider_id,
            request.model_name,
            StopOutcome.STOP,
            ModelUsage(11, 7, 18),
        )


def build_execution_service(
    engine: Engine,
    completions: dict[str, ModelCompletion],
    clock: Callable[[], datetime] | None = None,
    policy: QueuePolicy = PRODUCTION_QUEUE_POLICY,
    tool_loop: ToolLoopHandler | None = None,
) -> tuple[SqlAlchemyJobExecutionPersistence, JobExecutionService]:
    """Compose the shipped execution service over disposable persistence.

    `tool_loop` is what production's composition root always wires and every pre-D7 test omitted: a
    Run whose snapshot allows tools is routed to the loop, and one that allows none keeps the
    unchanged single-call path. Passing `None` reproduces the tool-free composition exactly.
    """
    persistence = SqlAlchemyJobExecutionPersistence(engine, policy=policy, sleep=lambda _: None)
    service = JobExecutionService(
        persistence,
        RunExecutor(create_builtin_handler_registry(), tool_loop=tool_loop),
        completions,
        clock or (lambda: NOW),
    )
    return persistence, service


def build_worker(
    engine: Engine,
    completions: dict[str, ModelCompletion],
    *,
    worker_id: str = "worker-1",
    concurrency: int = 1,
    max_active: int = 4,
    poll_interval: float = 0.01,
    idle_max: float = 0.02,
    shutdown_drain_seconds: float = 0.5,
    shutdown_grace: float = 2.0,
    heartbeat_interval: float = 3600.0,
    reclaim_interval: float = 3600.0,
    register: bool = True,
    clock: Callable[[], datetime] | None = None,
    policy: QueuePolicy = PRODUCTION_QUEUE_POLICY,
    tool_loop: ToolLoopHandler | None = None,
) -> Worker:
    """Compose the shipped Worker loop over disposable persistence.

    The registry and reclaimer are real, not doubles, so every Worker test exercises the shipped
    registration path. Both auxiliary loops default to a very long interval so they stay out of
    the way of tests that are not about them.

    `tool_loop` mirrors production's composition root so a Worker-composed suite can carry a
    tool-enabled Run through the same `RunExecutor` routing decision the process uses.
    """
    now = clock or (lambda: NOW)
    # The orchestration clock must be the same object the Worker uses: a start committed with a
    # clock behind the claim's own heartbeat would violate the ordering the schema enforces.
    _persistence, execution = build_execution_service(engine, completions, now, policy, tool_loop)
    registry = WorkerRegistry(_persistence, worker_id, clock=now)
    worker = Worker(
        _persistence,
        execution,
        completions,
        clock=now,
        worker_id=worker_id,
        concurrency=concurrency,
        max_active=max_active,
        registry=registry,
        reclaimer=ReclaimLoop(LeaseReclaimer(_persistence), clock=now),
        poll_interval=poll_interval,
        idle_max=idle_max,
        shutdown_grace=shutdown_grace,
        shutdown_drain_seconds=shutdown_drain_seconds,
        heartbeat_interval=heartbeat_interval,
        reclaim_interval=reclaim_interval,
    )
    # Register only once the Worker itself has accepted its configuration, so an invalid
    # concurrency/max_active fails on validation rather than on a database call.
    if register:
        registry.register()
    return worker


async def run_until_stopped(worker: Worker, engine: Engine) -> None:
    """Run the Worker until every submitted Run reaches a terminal state, then stop."""
    import asyncio

    stop = asyncio.Event()

    async def watch() -> None:
        for _ in range(2000):
            await asyncio.sleep(0.02)
            with engine.connect() as connection:
                active = int(
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM jobs"
                            " WHERE status IN ('queued','claimed','running')"
                        )
                    )
                    or 0
                )
            if active == 0:
                break
        stop.set()

    await asyncio.gather(worker.run(stop), watch())


async def run_until_job_status(worker: Worker, engine: Engine, status: str) -> None:
    """Run the Worker until some Job reaches `status`, then stop.

    C4 needs this because `retry_wait` is deliberately *not* terminal: a watcher that stops when
    no Job is active would tear the Worker down mid-wait and prove nothing about the schedule.
    """
    import asyncio

    stop = asyncio.Event()

    async def watch() -> None:
        for _ in range(2000):
            await asyncio.sleep(0.02)
            with engine.connect() as connection:
                matched = int(
                    connection.scalar(
                        text("SELECT count(*) FROM jobs WHERE status=:s"), {"s": status}
                    )
                    or 0
                )
            if matched:
                break
        stop.set()

    await asyncio.gather(worker.run(stop), watch())


async def run_until_all_jobs_terminal(worker: Worker, engine: Engine) -> None:
    """Run the Worker until no Job remains queued, claimed, running, or waiting to retry."""
    import asyncio

    stop = asyncio.Event()

    async def watch() -> None:
        for _ in range(2000):
            await asyncio.sleep(0.02)
            with engine.connect() as connection:
                pending = int(
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM jobs WHERE status IN"
                            " ('queued','claimed','running','retry_wait')"
                        )
                    )
                    or 0
                )
            if pending == 0:
                break
        stop.set()

    await asyncio.gather(worker.run(stop), watch())
