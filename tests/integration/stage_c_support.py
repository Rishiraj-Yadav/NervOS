"""Shared deterministic harness for the C8 integrated Stage C acceptance suites.

C8 proves composition rather than adding capability, so every helper here composes the *real*
modules earlier milestones shipped -- the durable submission primitive, the fenced execution
persistence, the reclamation and retry engines, the Worker supervisor, and C7's Event readers. No
scenario re-implements a transition by hand, because a hand-written fixture could agree with itself
while disagreeing with production.

Time is a parameter, never a wait. A lease expires by passing a later `now`, a retry becomes due by
passing a later `now`, and only the Worker-driven journeys use the real clock -- because there the
process loop itself is the thing under test.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from nervos_core.application.agents import EVENT_PAGE_LIMIT_MAX
from nervos_core.application.job_execution import (
    LEASE_DURATION,
    ClaimedAttempt,
    JobExecutionService,
)
from nervos_core.application.lease_reclamation import (
    LeaseReclaimer,
    ReclaimedClaim,
)
from nervos_core.application.model_completion import (
    MODEL_RATE_LIMITED,
    ModelCompletion,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    StopOutcome,
    safe_error_message,
)
from nervos_core.application.queue_policy import PRODUCTION_QUEUE_POLICY, QueuePolicy
from nervos_core.application.retry_policy import PRODUCTION_RETRY_POLICY
from nervos_core.application.run_execution import RunExecutor
from nervos_core.application.trusted_chat import create_builtin_handler_registry
from nervos_core.domain.jobs import RetryDisposition
from nervos_core.domain.runs import STAGE_B_LIMITS
from nervos_core.infrastructure.database import create_sqlite_engine
from nervos_core.infrastructure.database.agents import SqlAlchemyAgentPersistence
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobExecutionPersistence,
    SqlAlchemyJobPersistence,
)
from nervos_worker.registry import ReclaimLoop, WorkerRegistry
from nervos_worker.service import Worker
from sqlalchemy import Engine, text
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 14, tzinfo=UTC)
PROVIDER_ID = "anthropic"
SECOND_PROVIDER_ID = "openai"
BOTH_PROVIDERS = (PROVIDER_ID, SECOND_PROVIDER_ID)
# Wide everywhere a scenario is not deliberately exercising one dimension.
WIDE = 64
PERMISSIVE = QueuePolicy(
    global_active_limit=WIDE, per_agent_active_limit=WIDE, per_provider_active_limit=WIDE
)


def migrate(
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    agents: int = 1,
    providers: tuple[str, ...] = (PROVIDER_ID,),
    owner_user_id: int = 1,
) -> Engine:
    """Create one disposable migrated database holding a user and `agents` Agent Instances.

    Providers cycle over `providers`, so a scenario can build a fleet whose Instances map onto
    distinct model providers without hand-writing SQL.
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
                    "VALUES(:o,'nervos.chat','1',:d,1,:p,:m,:n,:n)"
                ),
                {
                    "o": owner_user_id,
                    "d": f"Agent {index}",
                    "p": provider,
                    "m": "opaque/model",
                    "n": NOW,
                },
            )
    return engine


# ---------------------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------------------


# These two helpers exist only for assertions about raw rows. Their result type is whatever SQLite
# returned for the selected column, so `Any` is the accurate annotation rather than a shortcut: a
# narrower one would be a guess the helper cannot make.
def rows(engine: Engine, query: str, **parameters: object) -> list[tuple[Any, ...]]:
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(text(query), parameters).all()]


def scalar(engine: Engine, query: str, **parameters: object) -> Any:
    with engine.connect() as connection:
        return connection.scalar(text(query), parameters)


def counts(engine: Engine) -> dict[str, int]:
    return {
        table: int(scalar(engine, f"SELECT count(*) FROM {table}") or 0)
        for table in ("runs", "jobs", "job_attempts", "run_events")
    }


def event_types(engine: Engine, run_id: int) -> list[str]:
    return [
        str(row[0])
        for row in rows(
            engine,
            "SELECT event_type FROM run_events WHERE run_id=:r ORDER BY sequence",
            r=run_id,
        )
    ]


def event_sequences(engine: Engine, run_id: int) -> list[int]:
    return [
        int(row[0])
        for row in rows(
            engine,
            "SELECT sequence FROM run_events WHERE run_id=:r ORDER BY sequence",
            r=run_id,
        )
    ]


def job_id_of(engine: Engine, run_id: int) -> int:
    return int(scalar(engine, "SELECT id FROM jobs WHERE run_id=:r", r=run_id) or 0)


def job_row(engine: Engine, run_id: int) -> dict[str, object]:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text(
                    "SELECT status, attempt_count, max_attempts, claimed_by, claim_token,"
                    " lease_expires_at, available_at, error_code, finished_at, cancel_requested_at"
                    " FROM jobs WHERE run_id=:r"
                ),
                {"r": run_id},
            )
            .mappings()
            .one()
        )


def run_row(engine: Engine, run_id: int) -> dict[str, object]:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text(
                    "SELECT status, started_at, finished_at, output_text, error_code,"
                    " error_message, elapsed_ms FROM runs WHERE id=:r"
                ),
                {"r": run_id},
            )
            .mappings()
            .one()
        )


def attempt_statuses(engine: Engine, run_id: int) -> list[str]:
    return [
        str(row[0])
        for row in rows(
            engine,
            "SELECT a.status FROM job_attempts a JOIN jobs j ON j.id = a.job_id"
            " WHERE j.run_id=:r ORDER BY a.attempt_number",
            r=run_id,
        )
    ]


def active_job_count(engine: Engine, *, now: datetime) -> int:
    """The exact predicate C6 compares against: a live lease is the only thing that counts."""
    return int(
        scalar(
            engine,
            "SELECT count(*) FROM jobs WHERE status IN ('claimed','running')"
            " AND lease_expires_at > :n",
            n=now,
        )
        or 0
    )


# The Agent-Instance dimension is identified by `agent_instances.id`; the provider dimension by
# its stored provider value. Both are read through the same live-lease predicate C6 compares.
ACTIVE_DIMENSIONS = {
    "agent_instance_id": "a.id",
    "model_provider": "a.model_provider",
}


def active_count_for(engine: Engine, *, dimension: str, value: object, now: datetime) -> int:
    """How many live leases one identity holds in one concurrency dimension."""
    predicate = ACTIVE_DIMENSIONS[dimension]
    return int(
        scalar(
            engine,
            "SELECT count(*) FROM jobs j JOIN runs r ON r.id = j.run_id"
            " JOIN agent_instances a ON a.id = r.agent_instance_id"
            " WHERE j.status IN ('claimed','running') AND j.lease_expires_at > :n"
            f" AND {predicate} = :v",
            n=now,
            v=value,
        )
        or 0
    )


def pending_job_count(engine: Engine, *, agent_instance_id: int | None = None) -> int:
    if agent_instance_id is None:
        return int(
            scalar(
                engine,
                "SELECT count(*) FROM jobs WHERE status IN"
                " ('queued','claimed','running','retry_wait')",
            )
            or 0
        )
    return int(
        scalar(
            engine,
            "SELECT count(*) FROM jobs j JOIN runs r ON r.id = j.run_id"
            " WHERE r.agent_instance_id = :a AND j.status IN"
            " ('queued','claimed','running','retry_wait')",
            a=agent_instance_id,
        )
        or 0
    )


def partition_rows(engine: Engine) -> dict[int, int | None]:
    """The durable fairness marker of every Agent partition that has a row."""
    return {
        int(row[0]): None if row[1] is None else int(row[1])
        for row in rows(
            engine,
            "SELECT agent_instance_id, last_served_attempt_id FROM queue_partitions"
            " ORDER BY agent_instance_id",
        )
    }


def live_attempt_violations(engine: Engine) -> int:
    """Jobs holding more than one Attempt that has not finished. Must always be zero."""
    return int(
        scalar(
            engine,
            "SELECT count(*) FROM (SELECT job_id FROM job_attempts"
            " WHERE status IN ('claimed','running') GROUP BY job_id HAVING count(*) > 1)",
        )
        or 0
    )


def runs_with_gapped_sequences(engine: Engine) -> list[int]:
    """Runs whose Event sequence is not a contiguous `1..N`, which would break the timeline."""
    gapped: list[int] = []
    for row in rows(engine, "SELECT DISTINCT run_id FROM run_events ORDER BY run_id"):
        run_id = int(row[0])
        observed = event_sequences(engine, run_id)
        if observed != list(range(1, len(observed) + 1)):
            gapped.append(run_id)
    return gapped


# ---------------------------------------------------------------------------------------
# The synchronous execution driver, on a caller-supplied clock
# ---------------------------------------------------------------------------------------


def submit(
    engine: Engine,
    *,
    agent: int = 1,
    text_value: str = "hello",
    max_attempts: int = 3,
    max_pending: int = 1000,
    now: datetime = NOW,
    owner_user_id: int = 1,
) -> int:
    return int(
        SqlAlchemyJobPersistence(engine, max_pending=max_pending)
        .submit(
            owner_user_id=owner_user_id,
            agent_instance_id=agent,
            input_text=text_value,
            limits=STAGE_B_LIMITS,
            now=now,
            max_attempts=max_attempts,
        )
        .id
    )


def execution(
    engine: Engine, *, policy: QueuePolicy = PERMISSIVE
) -> SqlAlchemyJobExecutionPersistence:
    return SqlAlchemyJobExecutionPersistence(engine, policy=policy, sleep=lambda _: None)


def claim(
    engine: Engine,
    *,
    worker_id: str = "worker-1",
    providers: tuple[str, ...] = (PROVIDER_ID,),
    max_active: int = WIDE,
    now: datetime = NOW,
    policy: QueuePolicy = PERMISSIVE,
) -> ClaimedAttempt | None:
    return execution(engine, policy=policy).claim_next(
        worker_id=worker_id,
        provider_ids=providers,
        max_active=max_active,
        now=now,
        lease_duration=LEASE_DURATION,
    )


def must_claim(engine: Engine, **kwargs: object) -> ClaimedAttempt:
    claimed = claim(engine, **kwargs)  # type: ignore[arg-type]
    assert claimed is not None, "a claim was expected but the queue refused"
    return claimed


def start(engine: Engine, claimed: ClaimedAttempt, *, now: datetime = NOW) -> ClaimedAttempt:
    assert execution(engine).start_attempt(claimed, now=now) is True
    return claimed


def succeed(
    engine: Engine,
    claimed: ClaimedAttempt,
    *,
    output_text: str = "done",
    elapsed_ms: int = 5,
    now: datetime = NOW,
) -> None:
    assert execution(engine).succeed(
        claimed,
        output_text=output_text,
        finish_reason="stop",
        usage=ModelUsage(1, 1, 2),
        elapsed_ms=elapsed_ms,
        now=now,
    )


def fail_rate_limited(engine: Engine, claimed: ClaimedAttempt, *, now: datetime = NOW) -> None:
    execution(engine).record_failure(
        claimed,
        error_code=MODEL_RATE_LIMITED,
        error_message=safe_error_message(MODEL_RATE_LIMITED),
        retry_disposition=RetryDisposition.SAFE_TO_RETRY,
        usage=ModelUsage(1, 1, 2),
        elapsed_ms=5,
        anchor_at=now,
        retry_policy=PRODUCTION_RETRY_POLICY,
        now=now,
    )


def expire(engine: Engine, *, backoff: timedelta, now: datetime) -> ReclaimedClaim | None:
    """Reclaim the next expired claim, exactly as a Worker's reclamation loop does."""
    return execution(engine).reclaim_next_expired_claim(now=now, backoff=backoff)


def run_events(engine: Engine, run_id: int, *, after_sequence: int = 0) -> list[tuple[int, str]]:
    """Read one Run's timeline through the C7 read path the browser uses."""
    events = SqlAlchemyAgentPersistence(sessionmaker(bind=engine)).list_run_events(
        run_id, after_sequence, EVENT_PAGE_LIMIT_MAX
    )
    return [(event.sequence, event.event_type.value) for event in events]


# ---------------------------------------------------------------------------------------
# Worker composition
# ---------------------------------------------------------------------------------------


class ScriptedCompletion:
    """A deterministic offline provider whose outcome per call index is declared up front.

    Nothing here is random: call `n` behaves exactly as scripted, so a scenario's provider-call
    count is a fact rather than an observation. Every call is recorded, which is what lets the
    acceptance suites assert that no call was made, or that exactly one was.
    """

    def __init__(
        self,
        provider_id: str = PROVIDER_ID,
        *,
        outcomes: Sequence[str] = ("succeed",),
        reply: str = "stage c answer",
    ) -> None:
        self.provider_id = provider_id
        self.outcomes = list(outcomes)
        self.reply = reply
        self.calls = 0
        self.requests: list[ModelRequest] = []

    @property
    def call_count(self) -> int:
        return self.calls

    async def complete(self, request: ModelRequest) -> ModelResponse:
        index = self.calls
        self.calls += 1
        self.requests.append(request)
        outcome = self.outcomes[index] if index < len(self.outcomes) else self.outcomes[-1]
        if outcome == "rate_limited":
            raise ModelProviderError(MODEL_RATE_LIMITED)
        return ModelResponse(
            self.reply,
            self.provider_id,
            request.model_name,
            StopOutcome.STOP,
            ModelUsage(11, 7, 18),
        )


@dataclass
class WorkerHarness:
    """One composed Worker and everything needed to drive and inspect it."""

    worker: Worker
    registry: WorkerRegistry
    reclaimer: ReclaimLoop
    engine: Engine
    completions: dict[str, ModelCompletion] = field(default_factory=dict[str, ModelCompletion])


def build_worker(
    engine: Engine,
    *,
    worker_id: str,
    completions: Mapping[str, ModelCompletion] | None = None,
    concurrency: int = 1,
    max_active: int = 16,
    policy: QueuePolicy = PERMISSIVE,
    register: bool = True,
    clock: Callable[[], datetime] | None = None,
) -> WorkerHarness:
    """Compose a Worker over the real supervisor, with its auxiliary loops parked out of the way.

    One persistence object is shared by the Worker, its registry, and its reclaimer, exactly as the
    process composition root does it. The heartbeat and reclamation intervals are set to an hour so
    a short scenario drives those transitions explicitly instead of racing a timer -- a suite that
    wants reclamation calls it, and does not hope it fired.
    """
    completion_map = completions or {PROVIDER_ID: ScriptedCompletion()}
    now = clock or (lambda: datetime.now(UTC))
    persistence = execution(engine, policy=policy)
    registry = WorkerRegistry(persistence, worker_id, clock=now)
    worker = Worker(
        persistence,
        JobExecutionService(
            persistence,
            RunExecutor(create_builtin_handler_registry()),
            completion_map,
            now,
        ),
        completion_map,
        clock=now,
        worker_id=worker_id,
        concurrency=concurrency,
        max_active=max_active,
        registry=registry,
        reclaimer=ReclaimLoop(LeaseReclaimer(persistence), clock=now),
        poll_interval=0.01,
        idle_max=0.02,
        shutdown_drain_seconds=0.5,
        shutdown_grace=2.0,
        heartbeat_interval=3600.0,
        reclaim_interval=3600.0,
    )
    if register:
        registry.register()
    return WorkerHarness(
        worker,
        registry,
        ReclaimLoop(LeaseReclaimer(persistence), clock=now),
        engine,
        dict(completion_map),
    )


async def run_until_settled(
    harnesses: Sequence[WorkerHarness],
    engine: Engine,
    *,
    polls: int = 3000,
    include_retry_wait: bool = False,
) -> None:
    """Run one or more Workers until no Job is left pending, then stop them.

    `retry_wait` is included when the scenario cares about it: a Job waiting out a backoff is not
    finished, and stopping a Worker in that window would prove nothing about whether it resumes.
    """
    pending_states = (
        "'queued','claimed','running','retry_wait'"
        if include_retry_wait
        else "'queued','claimed','running'"
    )
    stop = asyncio.Event()

    async def watch() -> None:
        for _ in range(polls):
            await asyncio.sleep(0.02)
            pending = int(
                scalar(engine, f"SELECT count(*) FROM jobs WHERE status IN ({pending_states})") or 0
            )
            if pending == 0:
                break
        stop.set()

    await asyncio.gather(*(harness.worker.run(stop) for harness in harnesses), watch())


def worker_states(engine: Engine, worker_id: str, *, now: datetime) -> dict[str, str]:
    """Derived registry health for every Worker row, keyed by id."""
    registry = WorkerRegistry(execution(engine), worker_id, clock=lambda: now)
    return {snapshot.worker_id: str(snapshot.state) for snapshot in registry.snapshots()}


def provider_ledger(completion: ScriptedCompletion) -> int:
    """The number of provider calls this double actually received."""
    return completion.call_count


__all__ = [
    "ACTIVE_DIMENSIONS",
    "BOTH_PROVIDERS",
    "EVENT_PAGE_LIMIT_MAX",
    "LEASE_DURATION",
    "NOW",
    "PERMISSIVE",
    "PRODUCTION_QUEUE_POLICY",
    "PROVIDER_ID",
    "ROOT",
    "SECOND_PROVIDER_ID",
    "WIDE",
    "ScriptedCompletion",
    "WorkerHarness",
    "active_count_for",
    "active_job_count",
    "attempt_statuses",
    "build_worker",
    "claim",
    "counts",
    "event_sequences",
    "event_types",
    "execution",
    "expire",
    "fail_rate_limited",
    "job_id_of",
    "job_row",
    "live_attempt_violations",
    "migrate",
    "must_claim",
    "partition_rows",
    "pending_job_count",
    "provider_ledger",
    "rows",
    "run_events",
    "run_row",
    "run_until_settled",
    "runs_with_gapped_sequences",
    "scalar",
    "start",
    "submit",
    "succeed",
    "worker_states",
]
