"""C6 fairness: least-recently-served selection between Agent-Instance partitions.

Every test drives the real claim transaction against a database built by the real migrations, so
the ordering under test is the ordering a Worker actually gets. Concurrency limits are lifted to
a permissive policy throughout, because this suite isolates *fairness*; the caps have their own
suite.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from execution_support import NOW, migrate, partition_rows
from nervos_core.application.job_execution import LEASE_DURATION, ClaimedAttempt
from nervos_core.application.model_completion import MODEL_RATE_LIMITED, safe_error_message
from nervos_core.application.queue_policy import QueuePolicy
from nervos_core.application.retry_policy import PRODUCTION_RETRY_POLICY
from nervos_core.domain.jobs import RetryDisposition
from nervos_core.domain.runs import STAGE_B_LIMITS, ModelUsage
from nervos_core.infrastructure.database import create_sqlite_engine
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobExecutionPersistence,
    SqlAlchemyJobPersistence,
    SqlAlchemyRunCancellationPersistence,
)
from sqlalchemy import Engine, text

ANTHROPIC = ("anthropic",)
OPENAI = ("openai",)
BOTH = ("anthropic", "openai")

# Fairness is tested with concurrency out of the way: every dimension is wide open, so a claim
# can only be refused because no partition is eligible.
PERMISSIVE = QueuePolicy(
    global_active_limit=64, per_agent_active_limit=64, per_provider_active_limit=64
)


def submit(engine: Engine, *, agent: int, text_value: str, now: datetime = NOW) -> int:
    """Submit one Run for `agent` and return its id."""
    return (
        SqlAlchemyJobPersistence(engine, max_pending=1000)
        .submit(
            owner_user_id=1,
            agent_instance_id=agent,
            input_text=text_value,
            limits=STAGE_B_LIMITS,
            now=now,
        )
        .id
    )


def execution(engine: Engine, *, policy: QueuePolicy = PERMISSIVE):
    return SqlAlchemyJobExecutionPersistence(engine, policy=policy, sleep=lambda _: None)


def claim(
    engine: Engine,
    *,
    worker_id: str = "worker-1",
    providers: tuple[str, ...] = BOTH,
    max_active: int = 64,
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


def started(engine: Engine, claimed: ClaimedAttempt, *, now: datetime = NOW) -> ClaimedAttempt:
    """Cross the execution-start boundary, as real orchestration does before settling a claim.

    Terminal settlement is fenced on a `running` Job, so a suite that never starts its Attempt
    would silently settle nothing.
    """
    assert execution(engine).start_attempt(claimed, now=now) is True
    return claimed


def job_status(engine: Engine, job_id: int) -> str:
    from execution_support import job_row

    return str(job_row(engine, job_id)["status"])


def repoint(engine: Engine, agent: int, provider: str) -> None:
    """Move one Agent Instance onto a provider, so a suite can build any capability mapping."""
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE agent_instances SET model_provider=:p WHERE id=:a"),
            {"p": provider, "a": agent},
        )


def serve(engine: Engine, run_agents: dict[int, int], claims: int, **kwargs: object) -> list[int]:
    """Claim `claims` times and return the Agent Instance that was served each time."""
    order: list[int] = []
    for _ in range(claims):
        claimed = claim(engine, **kwargs)  # type: ignore[arg-type]
        assert claimed is not None
        order.append(run_agents[claimed.run_id])
    return order


# ---------------------------------------------------------------------------------------
# The core guarantee
# ---------------------------------------------------------------------------------------


def test_one_backlogged_agent_cannot_monopolise_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three continuously-eligible partitions are served in strict rotation.

    This is the property C6 exists for. Before it, the candidate query was a global FIFO on
    `(available_at, id)`, so the Agent holding the oldest Jobs won every claim until it drained.
    """
    engine = migrate(tmp_path / "fair.db", monkeypatch, agents=3)
    try:
        run_agents: dict[int, int] = {}
        for agent in (1, 2, 3):
            for index in range(3):
                run_agents[submit(engine, agent=agent, text_value=f"a{agent}-{index}")] = agent

        order = serve(engine, run_agents, 9)

        assert order == [1, 2, 3, 1, 2, 3, 1, 2, 3]
        # Every partition was genuinely eligible throughout: none drained.
        assert set(partition_rows(engine)) == {1, 2, 3}
    finally:
        engine.dispose()


def test_a_narrow_worker_does_not_starve_a_partition_it_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The externally reviewed counterexample, kept as a regression test.

    NARROW can only run `anthropic`, so its eligible set is {2}. WIDE can only run `openai`, so
    its eligible set is {1, 3}. An ordering derived from the most recent *global* Attempt would
    leave WIDE forever computing "the first partition after 2" -- always 3 -- so Agent 1, which is
    continuously eligible to WIDE, would never be served. Durable per-partition history is what
    removes that failure, so Agent 1 and Agent 3 must alternate.
    """
    engine = migrate(tmp_path / "heterogeneous.db", monkeypatch, agents=3, providers=("openai",))
    try:
        repoint(engine, 2, "anthropic")
        run_agents: dict[int, int] = {}
        for agent in (1, 3):
            for index in range(4):
                run_agents[submit(engine, agent=agent, text_value=f"a{agent}-{index}")] = agent
        for index in range(4):
            run_agents[submit(engine, agent=2, text_value=f"narrow-{index}")] = 2

        wide: list[int] = []
        for _ in range(4):
            narrow = claim(engine, worker_id="narrow", providers=ANTHROPIC)
            assert narrow is not None and run_agents[narrow.run_id] == 2
            claimed = claim(engine, worker_id="wide", providers=OPENAI)
            assert claimed is not None
            wide.append(run_agents[claimed.run_id])

        assert wide == [1, 3, 1, 3]
    finally:
        engine.dispose()


def test_a_never_served_partition_sorts_ahead_of_a_served_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "never-served.db", monkeypatch, agents=2)
    try:
        run_agents = {submit(engine, agent=1, text_value="serves-agent-1"): 1}
        first = claim(engine)
        assert first is not None and run_agents[first.run_id] == 1

        # Agent 2 has never been served, so its Job goes ahead of a newer Agent 1 Job.
        run_agents[submit(engine, agent=1, text_value="second-for-agent-1")] = 1
        run_agents[submit(engine, agent=2, text_value="first-for-agent-2")] = 2

        claimed = claim(engine)

        assert claimed is not None
        assert run_agents[claimed.run_id] == 2
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# Within-partition ordering
# ---------------------------------------------------------------------------------------


def test_within_a_partition_the_oldest_due_job_is_served_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "within.db", monkeypatch, agents=1)
    try:
        first = submit(engine, agent=1, text_value="oldest", now=NOW)
        second = submit(engine, agent=1, text_value="middle", now=NOW + timedelta(seconds=5))
        third = submit(engine, agent=1, text_value="newest", now=NOW + timedelta(seconds=9))

        order: list[int] = []
        for _ in range(3):
            claimed = claim(engine, now=NOW + timedelta(seconds=10))
            assert claimed is not None
            order.append(claimed.run_id)

        assert order == [first, second, third]
    finally:
        engine.dispose()


def test_the_only_eligible_partition_is_served_every_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "sole.db", monkeypatch, agents=2)
    try:
        repoint(engine, 2, "openai")
        run_agents: dict[int, int] = {}
        for index in range(3):
            run_agents[submit(engine, agent=1, text_value=f"only-{index}")] = 1
        run_agents[submit(engine, agent=2, text_value="unreachable")] = 2

        assert serve(engine, run_agents, 3, providers=ANTHROPIC) == [1, 1, 1]
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# Skipping: saturation and capability
# ---------------------------------------------------------------------------------------


def test_a_provider_saturated_partition_is_skipped_rather_than_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agent 1 still has eligible work, but its provider is at its cap, so Agent 2 goes."""
    engine = migrate(tmp_path / "provider-saturated.db", monkeypatch, agents=2, providers=BOTH)
    try:
        run_agents: dict[int, int] = {}
        for index in range(2):
            run_agents[submit(engine, agent=1, text_value=f"a1-{index}")] = 1
        run_agents[submit(engine, agent=2, text_value="a2")] = 2

        capped = QueuePolicy(
            global_active_limit=64, per_agent_active_limit=64, per_provider_active_limit=1
        )
        first = claim(engine, policy=capped)
        assert first is not None and run_agents[first.run_id] == 1
        second = claim(engine, policy=capped)
        assert second is not None and run_agents[second.run_id] == 2
    finally:
        engine.dispose()


def test_an_agent_saturated_partition_is_skipped_rather_than_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "agent-saturated.db", monkeypatch, agents=2)
    try:
        run_agents: dict[int, int] = {}
        for index in range(2):
            run_agents[submit(engine, agent=1, text_value=f"a1-{index}")] = 1
        run_agents[submit(engine, agent=2, text_value="a2")] = 2

        capped = QueuePolicy(
            global_active_limit=64, per_agent_active_limit=1, per_provider_active_limit=64
        )
        first = claim(engine, policy=capped)
        assert first is not None and run_agents[first.run_id] == 1
        second = claim(engine, policy=capped)
        assert second is not None and run_agents[second.run_id] == 2
    finally:
        engine.dispose()


def test_a_skipped_partition_re_enters_at_its_own_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partition blocked by a cap sits outside the eligible set and loses nothing by waiting.

    Agent 1 was served first, so its marker is the older one. Once its cap frees, Agent 1 must be
    served again: if the skip had penalised its position, Agent 2 would go first instead.
    """
    engine = migrate(tmp_path / "re-entry.db", monkeypatch, agents=2)
    try:
        run_agents: dict[int, int] = {}
        for index in range(2):
            run_agents[submit(engine, agent=1, text_value=f"a1-{index}")] = 1
        run_agents[submit(engine, agent=2, text_value="a2")] = 2

        capped = QueuePolicy(
            global_active_limit=64, per_agent_active_limit=1, per_provider_active_limit=64
        )
        first = claim(engine, policy=capped)
        assert first is not None and run_agents[first.run_id] == 1
        second = claim(engine, policy=capped)
        assert second is not None and run_agents[second.run_id] == 2
        markers = partition_rows(engine)
        first_marker, second_marker = markers[1], markers[2]
        assert first_marker is not None and second_marker is not None
        assert first_marker < second_marker

        started(engine, first)
        assert execution(engine).succeed(
            first,
            output_text="done",
            finish_reason="stop",
            usage=ModelUsage(1, 1, 2),
            elapsed_ms=5,
            now=NOW + timedelta(seconds=1),
        )

        third = claim(engine, policy=capped, now=NOW + timedelta(seconds=1))
        assert third is not None and run_agents[third.run_id] == 1
    finally:
        engine.dispose()


def test_an_unsupported_provider_partition_is_skipped_and_keeps_its_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Worker that cannot run a partition must neither serve it nor advance it."""
    engine = migrate(tmp_path / "capability.db", monkeypatch, agents=2, providers=("openai",))
    try:
        repoint(engine, 2, "anthropic")
        run_agents = {
            submit(engine, agent=1, text_value="openai-and-older"): 1,
            submit(engine, agent=2, text_value="anthropic-only"): 2,
        }

        claimed = claim(engine, providers=ANTHROPIC)

        assert claimed is not None and run_agents[claimed.run_id] == 2
        # Nothing advanced Agent 1, so no fairness row exists for it yet.
        assert set(partition_rows(engine)) == {2}
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# retry_wait, cancellation, and C3 recovery
# ---------------------------------------------------------------------------------------


def rate_limited(engine: Engine, held: ClaimedAttempt, *, now: datetime = NOW) -> None:
    execution(engine).record_failure(
        held,
        error_code=MODEL_RATE_LIMITED,
        error_message=safe_error_message(MODEL_RATE_LIMITED),
        retry_disposition=RetryDisposition.SAFE_TO_RETRY,
        usage=ModelUsage(1, 1, 2),
        elapsed_ms=5,
        anchor_at=now,
        retry_policy=PRODUCTION_RETRY_POLICY,
        now=now,
    )


def test_a_future_retry_is_not_eligible_and_does_not_block_another_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "retry-future.db", monkeypatch, agents=2)
    try:
        run_agents = {submit(engine, agent=1, text_value="will-retry"): 1}
        run_agents[submit(engine, agent=2, text_value="ready-now")] = 2

        held = claim(engine)
        assert held is not None and run_agents[held.run_id] == 1
        started(engine, held)
        rate_limited(engine, held)
        assert job_status(engine, held.job_id) == "retry_wait"

        # The retry is due one second after its anchor; before then Agent 1 has no eligible work.
        early = claim(engine, now=NOW + timedelta(milliseconds=500))
        assert early is not None and run_agents[early.run_id] == 2
    finally:
        engine.dispose()


def test_a_due_retry_joins_fairness_on_the_same_terms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After its due instant a retry is ordinary work: no retry lane and no retry priority."""
    engine = migrate(tmp_path / "retry-due.db", monkeypatch, agents=2)
    try:
        run_agents = {submit(engine, agent=1, text_value="will-retry"): 1}
        run_agents[submit(engine, agent=2, text_value="ready-now")] = 2

        held = claim(engine)
        assert held is not None and run_agents[held.run_id] == 1
        started(engine, held)
        rate_limited(engine, held)
        assert job_status(engine, held.job_id) == "retry_wait"

        second = claim(engine, now=NOW + timedelta(seconds=1))
        assert second is not None and run_agents[second.run_id] == 2

        later = claim(engine, now=NOW + timedelta(seconds=30))

        assert later is not None
        assert run_agents[later.run_id] == 1
        assert later.attempt_number == 2
    finally:
        engine.dispose()


def test_a_cancelled_partition_is_never_a_fairness_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "cancelled.db", monkeypatch, agents=2)
    try:
        run_agents = {submit(engine, agent=1, text_value="cancelled"): 1}
        cancelled_run = next(iter(run_agents))
        run_agents[submit(engine, agent=2, text_value="runnable")] = 2
        SqlAlchemyRunCancellationPersistence(engine, sleep=lambda _: None).cancel_run(
            user_id=1, run_id=cancelled_run, now=NOW + timedelta(seconds=1)
        )

        claimed = claim(engine)

        assert claimed is not None and run_agents[claimed.run_id] == 2
        # Cancelling wrote nothing to the fairness history.
        assert 1 not in partition_rows(engine)
    finally:
        engine.dispose()


def test_a_recovered_pre_start_job_rejoins_only_once_due(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C3 requeues behind its reclaim backoff; until then the partition is simply not eligible."""
    engine = migrate(tmp_path / "recovered.db", monkeypatch, agents=2)
    try:
        run_agents = {submit(engine, agent=1, text_value="loses-its-worker"): 1}
        run_agents[submit(engine, agent=2, text_value="ready-now")] = 2

        lost = claim(engine)
        assert lost is not None and run_agents[lost.run_id] == 1
        recovered_at = NOW + LEASE_DURATION + timedelta(seconds=1)
        reclaimed = execution(engine).reclaim_next_expired_claim(
            now=recovered_at, backoff=timedelta(seconds=5)
        )
        assert reclaimed is not None

        early = claim(engine, now=recovered_at + timedelta(seconds=1))
        assert early is not None and run_agents[early.run_id] == 2
        later = claim(engine, now=recovered_at + timedelta(seconds=6))
        assert later is not None and run_agents[later.run_id] == 1
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# Advancement, durability, and multi-Worker safety
# ---------------------------------------------------------------------------------------


def test_fairness_advances_only_for_a_committed_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poll that finds nothing must not move any partition's position."""
    engine = migrate(tmp_path / "no-advance.db", monkeypatch, agents=2, providers=("openai",))
    try:
        repoint(engine, 2, "anthropic")
        run_agents = {submit(engine, agent=2, text_value="anthropic-only"): 2}

        # Agent 1 has no work at all, and this Worker cannot run `openai` in any case.
        assert claim(engine, providers=OPENAI) is None
        assert claim(engine, providers=OPENAI) is None
        assert partition_rows(engine) == {}

        claimed = claim(engine, providers=ANTHROPIC)

        assert claimed is not None and run_agents[claimed.run_id] == 2
        assert partition_rows(engine) == {2: claimed.attempt_id}
    finally:
        engine.dispose()


def test_the_fairness_order_survives_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reopened database continues the rotation instead of resetting it.

    If the ordering lived in process memory, both partitions would look never-served on restart
    and the Agent-id tie-break would serve Agent 1 again. Persistence is what makes Agent 2's turn
    survive the gap.
    """
    engine = migrate(tmp_path / "restart.db", monkeypatch, agents=2)
    run_agents: dict[int, int] = {}
    for agent in (1, 2):
        for index in range(2):
            run_agents[submit(engine, agent=agent, text_value=f"a{agent}-{index}")] = agent

    first = claim(engine)
    assert first is not None and run_agents[first.run_id] == 1
    expected_markers = partition_rows(engine)
    engine.dispose()

    reopened = create_sqlite_engine(tmp_path / "restart.db")
    try:
        assert partition_rows(reopened) == expected_markers
        second = claim(reopened, worker_id="worker-2")
        assert second is not None and run_agents[second.run_id] == 2
    finally:
        reopened.dispose()


def test_concurrent_workers_cannot_corrupt_the_fairness_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two Workers claiming at once never leave a marker that contradicts the Attempts.

    The marker is written inside the same serialized transaction that inserts its Attempt, so a
    race cannot lose an update: each partition's recorded marker must be the id of a real Attempt
    that partition holds. This asserts the invariant rather than a specific winner, because which
    Worker wins the race is genuinely unspecified -- that one-winner property is C2's suite.
    """
    engine = migrate(tmp_path / "fair-race.db", monkeypatch, agents=2)
    try:
        run_agents: dict[int, int] = {}
        for agent in (1, 2):
            for index in range(3):
                run_agents[submit(engine, agent=agent, text_value=f"a{agent}-{index}")] = agent

        results: list[ClaimedAttempt | None] = [None, None]
        barrier = threading.Barrier(2)

        def attempt(index: int) -> None:
            local = create_sqlite_engine(tmp_path / "fair-race.db")
            try:
                barrier.wait(timeout=10)
                results[index] = claim(local, worker_id=f"worker-{index}")
            finally:
                local.dispose()

        threads = [threading.Thread(target=attempt, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        claimed = [item for item in results if item is not None]
        assert claimed, "at least one Worker must win the race"
        # No Job was claimed twice.
        assert len({item.job_id for item in claimed}) == len(claimed)

        markers = partition_rows(engine)
        with engine.connect() as connection:
            for agent, marker in markers.items():
                assert marker == int(
                    connection.scalar(
                        text(
                            "SELECT MAX(job_attempts.id) FROM job_attempts"
                            " JOIN jobs ON jobs.id = job_attempts.job_id"
                            " WHERE jobs.agent_instance_id = :a"
                        ),
                        {"a": agent},
                    )
                )
    finally:
        engine.dispose()
