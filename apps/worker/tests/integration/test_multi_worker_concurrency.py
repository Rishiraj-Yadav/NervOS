"""C6 multi-Worker concurrency: the caps hold when real Workers claim at the same time.

The C6 caps are decided inside one serialized claim transaction, so the property worth proving at
this level is not "one winner" (that is C2's suite) but that *observed* concurrency never exceeds
the authoritative limit while two real Worker loops poll the same database. Each provider call
reads the live-claim count at the one moment its own claim is definitely live.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from nervos_core.application.job_execution import LEASE_DURATION
from nervos_core.application.model_completion import (
    ModelCompletion,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    StopOutcome,
)
from nervos_core.application.queue_policy import PRODUCTION_QUEUE_POLICY, QueuePolicy
from nervos_core.infrastructure.database import create_sqlite_engine
from nervos_core.infrastructure.database.jobs import SqlAlchemyJobExecutionPersistence
from sqlalchemy import Engine, text
from support import (
    NOW,
    PROVIDER_ID,
    Worker,
    build_worker,
    counts,
    job_row,
    migrate,
    run_row,
    submit,
)

ANTHROPIC = "anthropic"
OPENAI = "openai"
WIDE = 64


def policy_for(
    *, global_limit: int = WIDE, agent_limit: int = WIDE, provider_limit: int = WIDE
) -> QueuePolicy:
    return QueuePolicy(
        global_active_limit=global_limit,
        per_agent_active_limit=agent_limit,
        per_provider_active_limit=provider_limit,
    )


class CapWatchingCompletion:
    """A provider double that records the live-claim counts its own call was running beside."""

    def __init__(self, database_path: Path, provider_id: str = PROVIDER_ID) -> None:
        self.provider_id = provider_id
        self._database_path = database_path
        self.calls = 0
        self.max_live_global = 0
        self.max_live_per_agent: dict[int, int] = {}

    def _live_claims(self) -> tuple[int, dict[int, int]]:
        engine = create_sqlite_engine(self._database_path)
        try:
            with engine.connect() as connection:
                rows = connection.execute(
                    text(
                        "SELECT agent_instance_id, count(*) FROM jobs"
                        " WHERE status IN ('claimed','running') GROUP BY agent_instance_id"
                    )
                ).all()
        finally:
            engine.dispose()
        per_agent = {int(row[0]): int(row[1]) for row in rows}
        return sum(per_agent.values()), per_agent

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        live, per_agent = await asyncio.to_thread(self._live_claims)
        self.max_live_global = max(self.max_live_global, live)
        for agent, count in per_agent.items():
            self.max_live_per_agent[agent] = max(self.max_live_per_agent.get(agent, 0), count)
        return ModelResponse(
            "worker answer",
            self.provider_id,
            request.model_name,
            StopOutcome.STOP,
            ModelUsage(11, 7, 18),
        )


async def run_workers_until_drained(
    workers: list[Worker], engine: Engine, *, polls: int = 3000
) -> None:
    """Run every Worker concurrently until no Job remains outstanding, then stop them all."""
    stop = asyncio.Event()

    async def watch() -> None:
        for _ in range(polls):
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

    await asyncio.gather(*(worker.run(stop) for worker in workers), watch())


def statuses(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        return [str(row[0]) for row in connection.execute(text("SELECT status FROM runs")).all()]


# ---------------------------------------------------------------------------------------
# Two Workers, one authoritative global cap
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_two_workers_never_exceed_the_authoritative_global_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both Workers are handed a budget far above the policy; the policy still binds."""
    engine = migrate(tmp_path / "two-workers-global.db", monkeypatch, agents=1)
    try:
        for index in range(3):
            submit(engine, text_value=f"job-{index}")
        watching = CapWatchingCompletion(tmp_path / "two-workers-global.db")
        completions: dict[str, ModelCompletion] = {PROVIDER_ID: watching}
        authoritative = policy_for(global_limit=1)
        workers = [
            build_worker(
                engine,
                completions,
                worker_id=f"worker-{index}",
                max_active=8,
                poll_interval=0.01,
                policy=authoritative,
            )
            for index in range(2)
        ]

        await run_workers_until_drained(workers, engine)

        assert statuses(engine) == ["succeeded"] * 3
        assert watching.calls == 3
        # Each call observed exactly one live claim: the ceiling held under real contention.
        assert watching.max_live_global == 1
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_two_workers_share_one_provider_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The provider bound is per provider value, so it binds across Agent Instances.

    Both Instances sit on the same provider, and the per-Agent budget is left wide. If the cap
    were counted per Agent rather than per provider, two Instances would run side by side.
    """
    engine = migrate(tmp_path / "two-workers-provider.db", monkeypatch, agents=2)
    try:
        for index in range(2):
            submit(engine, text_value=f"agent-1-{index}", agent_instance_id=1)
            submit(engine, text_value=f"agent-2-{index}", agent_instance_id=2)
        watching = CapWatchingCompletion(tmp_path / "two-workers-provider.db")
        completions: dict[str, ModelCompletion] = {PROVIDER_ID: watching}
        capped = policy_for(provider_limit=1)
        workers = [
            build_worker(
                engine,
                completions,
                worker_id=f"worker-{index}",
                max_active=8,
                poll_interval=0.01,
                policy=capped,
            )
            for index in range(2)
        ]

        await run_workers_until_drained(workers, engine)

        assert statuses(engine) == ["succeeded"] * 4
        assert watching.calls == 4
        assert watching.max_live_global == 1
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_two_workers_share_the_per_agent_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each Instance is bounded by its own budget, and neither budget limits the other."""
    engine = migrate(tmp_path / "two-workers-agent.db", monkeypatch, agents=2)
    try:
        for index in range(2):
            submit(engine, text_value=f"agent-1-{index}", agent_instance_id=1)
            submit(engine, text_value=f"agent-2-{index}", agent_instance_id=2)
        watching = CapWatchingCompletion(tmp_path / "two-workers-agent.db")
        completions: dict[str, ModelCompletion] = {PROVIDER_ID: watching}
        capped = policy_for(agent_limit=1)
        workers = [
            build_worker(
                engine,
                completions,
                worker_id=f"worker-{index}",
                max_active=8,
                poll_interval=0.01,
                policy=capped,
            )
            for index in range(2)
        ]

        await run_workers_until_drained(workers, engine)

        assert statuses(engine) == ["succeeded"] * 4
        assert watching.calls == 4
        # Both Instances made progress, and each was observed holding at most its own one slot.
        assert set(watching.max_live_per_agent) == {1, 2}
        assert all(count == 1 for count in watching.max_live_per_agent.values()), (
            watching.max_live_per_agent
        )
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# Disjoint provider capabilities
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_workers_with_disjoint_capabilities_divide_the_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each Worker runs only its own provider, and together they drain both backlogs."""
    engine = migrate(tmp_path / "disjoint.db", monkeypatch, agents=2, providers=(ANTHROPIC, OPENAI))
    try:
        for index in range(2):
            submit(engine, text_value=f"anthropic-{index}", agent_instance_id=1)
            submit(engine, text_value=f"openai-{index}", agent_instance_id=2)
        anthropic = CapWatchingCompletion(tmp_path / "disjoint.db", ANTHROPIC)
        openai_double = CapWatchingCompletion(tmp_path / "disjoint.db", OPENAI)
        workers = [
            build_worker(
                engine,
                {ANTHROPIC: anthropic},  # type: ignore[dict-item]
                worker_id="anthropic-worker",
                max_active=4,
                poll_interval=0.01,
            ),
            build_worker(
                engine,
                {OPENAI: openai_double},  # type: ignore[dict-item]
                worker_id="openai-worker",
                max_active=4,
                poll_interval=0.01,
            ),
        ]

        await run_workers_until_drained(workers, engine)

        assert statuses(engine) == ["succeeded"] * 4
        # Neither Worker ever executed work belonging to the other's provider.
        assert anthropic.calls == 2
        assert openai_double.calls == 2
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# Simultaneous polling over a multi-Agent backlog
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_simultaneous_polling_drains_every_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fairness and concurrency together: every partition progresses and no Run is left behind."""
    engine = migrate(tmp_path / "fair-drain.db", monkeypatch, agents=3)
    try:
        for agent in (1, 2, 3):
            for index in range(3):
                submit(engine, text_value=f"a{agent}-{index}", agent_instance_id=agent)
        watching = CapWatchingCompletion(tmp_path / "fair-drain.db")
        completions: dict[str, ModelCompletion] = {PROVIDER_ID: watching}
        workers = [
            build_worker(
                engine,
                completions,
                worker_id=f"worker-{index}",
                max_active=4,
                poll_interval=0.01,
            )
            for index in range(2)
        ]

        await run_workers_until_drained(workers, engine)

        assert statuses(engine) == ["succeeded"] * 9
        assert watching.calls == 9
        with engine.connect() as connection:
            served = [
                int(row[0])
                for row in connection.execute(
                    text(
                        "SELECT agent_instance_id FROM queue_partitions ORDER BY agent_instance_id"
                    )
                ).all()
            ]
        # Every partition was actually served, so none was starved out of its turn.
        assert served == [1, 2, 3]
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# An expired claim
# ---------------------------------------------------------------------------------------


def test_an_expired_claim_frees_its_slot_and_stays_out_of_reach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An expired lease stops consuming concurrency, and C3 -- not a claim -- owns it."""
    engine = migrate(tmp_path / "expired.db", monkeypatch, agents=2)
    try:
        submit(engine, text_value="will-expire", agent_instance_id=1)
        submit(engine, text_value="other-partition", agent_instance_id=2)
        capped = policy_for(global_limit=1)
        persistence = SqlAlchemyJobExecutionPersistence(engine, policy=capped, sleep=lambda _: None)

        lost = persistence.claim_next(
            worker_id="dead-worker",
            provider_ids=(PROVIDER_ID,),
            max_active=8,
            now=NOW,
            lease_duration=timedelta(seconds=1),
        )
        assert lost is not None
        # While the lease is live the single slot is taken.
        assert (
            persistence.claim_next(
                worker_id="live-worker",
                provider_ids=(PROVIDER_ID,),
                max_active=8,
                now=NOW,
                lease_duration=LEASE_DURATION,
            )
            is None
        )

        # Once it expires the slot frees, and the freed slot goes to the *other* partition.
        expired_at = NOW + timedelta(seconds=2)
        recovered_work = persistence.claim_next(
            worker_id="live-worker",
            provider_ids=(PROVIDER_ID,),
            max_active=8,
            now=expired_at,
            lease_duration=LEASE_DURATION,
        )
        assert recovered_work is not None
        assert recovered_work.job_id != lost.job_id
        assert job_row(engine, lost.job_id)["status"] == "claimed"

        # C3 reclamation is the only path that may move the expired claim.
        reclaimed = persistence.reclaim_next_expired_claim(
            now=expired_at + timedelta(seconds=1), backoff=timedelta(seconds=5)
        )
        assert reclaimed is not None
        assert job_row(engine, lost.job_id)["status"] == "queued"
        assert counts(engine)["job_attempts"] == 2
    finally:
        engine.dispose()


def test_the_production_policy_is_the_claim_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Worker composed without an explicit policy gets the shared authoritative limits."""
    engine = migrate(tmp_path / "defaults.db", monkeypatch, agents=1)
    try:
        worker = build_worker(engine, {}, worker_id="defaults-worker", max_active=8)
        assert isinstance(worker, Worker)
        for index in range(PRODUCTION_QUEUE_POLICY.global_active_limit + 1):
            submit(engine, text_value=f"job-{index}")
        persistence = SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None)
        for index in range(PRODUCTION_QUEUE_POLICY.global_active_limit):
            assert (
                persistence.claim_next(
                    worker_id="probe",
                    provider_ids=(PROVIDER_ID,),
                    max_active=8,
                    now=NOW,
                    lease_duration=LEASE_DURATION,
                )
                is not None
            ), index
        assert (
            persistence.claim_next(
                worker_id="probe",
                provider_ids=(PROVIDER_ID,),
                max_active=8,
                now=NOW,
                lease_duration=LEASE_DURATION,
            )
            is None
        )
        assert run_row(engine, 1)["status"] == "created"
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# The deterministic C6 acceptance proof
# ---------------------------------------------------------------------------------------


class OrderRecordingCompletion:
    """A provider double that records the input of every call, in the order it was asked."""

    def __init__(self, provider_id: str = PROVIDER_ID) -> None:
        self.provider_id = provider_id
        self.order: list[str] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.order.append(request.user_text)
        return ModelResponse(
            "worker answer",
            self.provider_id,
            request.model_name,
            StopOutcome.STOP,
            ModelUsage(11, 7, 18),
        )


@pytest.mark.anyio
async def test_a_newcomer_agent_is_not_blocked_by_an_older_backlog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The C6 acceptance proof: a continuously-eligible newcomer is served ahead of a backlog.

    Agent 1 submits six Runs first, so every one of them is older than anything Agent 2 has.
    Under the pre-C6 global FIFO on `(available_at, id)` Agent 2 would not be served at all until
    Agent 1 drained. Least-recently-served fairness must interleave them, and no cap may be
    exceeded while it does.
    """
    engine = migrate(tmp_path / "acceptance.db", monkeypatch, agents=2)
    try:
        for index in range(6):
            submit(engine, text_value=f"a1-{index}", agent_instance_id=1)
        for index in range(2):
            submit(engine, text_value=f"a2-{index}", agent_instance_id=2)

        recording = OrderRecordingCompletion()
        completions: dict[str, ModelCompletion] = {PROVIDER_ID: recording}
        workers = [
            build_worker(
                engine,
                completions,
                worker_id="acceptance-worker",
                concurrency=1,
                max_active=1,
                poll_interval=0.01,
                policy=policy_for(global_limit=1),
            )
        ]

        await run_workers_until_drained(workers, engine)

        # Agent 2 was served twice while Agent 1 still had four Runs outstanding.
        assert recording.order[:4] == ["a1-0", "a2-0", "a1-1", "a2-1"]
        assert sorted(recording.order) == sorted(
            [f"a1-{index}" for index in range(6)] + [f"a2-{index}" for index in range(2)]
        )
        assert statuses(engine) == ["succeeded"] * 8
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT max(attempt_count) FROM jobs")) == 1
        # Both partitions were truly served, and no Run was left behind.
        assert sorted(markers := _served_partitions(engine)) == [1, 2]
        assert len(markers) == 2
    finally:
        engine.dispose()


def _served_partitions(engine: Engine) -> list[int]:
    with engine.connect() as connection:
        return [
            int(row[0])
            for row in connection.execute(
                text("SELECT agent_instance_id FROM queue_partitions ORDER BY agent_instance_id")
            ).all()
        ]
