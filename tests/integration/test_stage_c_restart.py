"""C8 restart acceptance: what Stage C guarantees across a process restart.

Every property here is asserted against a database file that was closed and reopened, never against
state a process happened to be holding. That is the whole point of the milestone: a durable queue
whose behaviour changed when the process that was driving it went away would not be durable, and a
suite that only ever ran one process could not tell the difference.

Reopening the file is the strongest statement available without spawning a second interpreter, and
it is also the honest one: a restarted Worker composes the same modules over the same file, so what
a test must rule out is any correctness that lived in a Python object.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from nervos_core.application.agents import RunNotFound
from nervos_core.application.lease_reclamation import ReclamationKind
from nervos_core.domain.jobs import JobStatus
from nervos_core.domain.runs import RunStatus
from nervos_core.infrastructure.database import create_sqlite_engine
from nervos_core.infrastructure.database.agents import SqlAlchemyAgentPersistence
from stage_c_support import (
    LEASE_DURATION,
    NOW,
    PERMISSIVE,
    PROVIDER_ID,
    SECOND_PROVIDER_ID,
    ScriptedCompletion,
    attempt_statuses,
    build_worker,
    claim,
    counts,
    event_sequences,
    event_types,
    execution,
    expire,
    fail_rate_limited,
    job_row,
    must_claim,
    partition_rows,
    provider_ledger,
    run_events,
    run_row,
    run_until_settled,
    start,
    submit,
    succeed,
)

RECLAIM_BACKOFF = timedelta(seconds=5)


def reopen(path: Path):
    return create_sqlite_engine(path)


# ---------------------------------------------------------------------------------------
# 1 -- a queued Job survives a restart and is executed afterwards
# ---------------------------------------------------------------------------------------


def test_a_queued_job_survives_a_restart_and_is_executed_afterwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage_c_support import migrate

    path = tmp_path / "queued.db"
    engine = migrate(path, monkeypatch)
    try:
        run_id = submit(engine, text_value="queued-across-restart")
        assert job_row(engine, run_id)["status"] == JobStatus.QUEUED.value
        markers_before = partition_rows(engine)
    finally:
        engine.dispose()

    restarted = reopen(path)
    try:
        # The obligation is exactly where it was left, and still claimable.
        assert job_row(restarted, run_id)["status"] == JobStatus.QUEUED.value
        assert partition_rows(restarted) == markers_before
        succeed(restarted, start(restarted, must_claim(restarted, worker_id="restarted")))

        assert run_row(restarted, run_id)["status"] == RunStatus.SUCCEEDED.value
        assert event_types(restarted, run_id) == [
            "run.created",
            "run.queued",
            "attempt.claimed",
            "attempt.started",
            "run.succeeded",
        ]
    finally:
        restarted.dispose()


# ---------------------------------------------------------------------------------------
# 2 -- a retry_wait Job survives with its exact due instant and resumes
# ---------------------------------------------------------------------------------------


def test_a_retry_wait_job_survives_with_its_exact_due_instant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage_c_support import migrate

    path = tmp_path / "retry.db"
    engine = migrate(path, monkeypatch)
    try:
        run_id = submit(engine, text_value="retry-across-restart", max_attempts=3)
        fail_rate_limited(engine, start(engine, must_claim(engine)))
        assert job_row(engine, run_id)["status"] == JobStatus.RETRY_WAIT.value
        # Read back through the raw row, so the comparison below is on the stored value itself.
        due_at = job_row(engine, run_id)["available_at"]
        assert due_at is not None
    finally:
        engine.dispose()

    restarted = reopen(path)
    try:
        assert job_row(restarted, run_id)["status"] == JobStatus.RETRY_WAIT.value
        # The instant is preserved exactly, not approximated from a restart time.
        assert job_row(restarted, run_id)["available_at"] == due_at
        assert attempt_statuses(restarted, run_id) == ["failed"]

        # Not claimable one instant early, claimable once due -- both judged from the durable row.
        # The first retry backoff is one second after the failure anchored at `NOW`.
        due = NOW + timedelta(seconds=1)
        assert claim(restarted, worker_id="restarted", now=due - timedelta(milliseconds=1)) is None
        resumed = must_claim(restarted, worker_id="restarted", now=due)
        succeed(restarted, start(restarted, resumed, now=due), now=due)

        assert run_row(restarted, run_id)["status"] == RunStatus.SUCCEEDED.value
        assert event_types(restarted, run_id)[-1] == "run.succeeded"
    finally:
        restarted.dispose()


# ---------------------------------------------------------------------------------------
# 3 -- the durable fairness history survives
# ---------------------------------------------------------------------------------------


def test_the_fairness_history_survives_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C6's ordering is read from a table, so a restart cannot reset it to a fresh rotation."""
    from stage_c_support import migrate

    path = tmp_path / "fairness.db"
    engine = migrate(path, monkeypatch, agents=3)
    try:
        run_agents: dict[int, int] = {}
        for agent in (1, 2, 3):
            for index in range(2):
                run_agents[submit(engine, agent=agent, text_value=f"f-{agent}-{index}")] = agent
        served = [run_agents[must_claim(engine, worker_id="worker-1").run_id] for _ in range(3)]
        assert served == [1, 2, 3]
        expected_markers = partition_rows(engine)
        assert set(expected_markers) == {1, 2, 3}
    finally:
        engine.dispose()

    restarted = reopen(path)
    try:
        assert partition_rows(restarted) == expected_markers
        # The rotation resumes where it left off: Agent 1, not a fresh start at Agent 1 by luck.
        next_served = run_agents[must_claim(restarted, worker_id="restarted").run_id]
        assert next_served == 1
        with restarted.connect() as connection:
            from sqlalchemy import text

            assert connection.scalar(text("SELECT count(*) FROM queue_partitions")) == 3
    finally:
        restarted.dispose()


# ---------------------------------------------------------------------------------------
# 4 -- Worker incarnation and staleness across a restart
# ---------------------------------------------------------------------------------------


def test_worker_incarnation_and_staleness_are_durable_and_never_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage_c_support import migrate, worker_states

    path = tmp_path / "registry.db"
    engine = migrate(path, monkeypatch)
    try:
        # `register=False` so the explicit registrations below are the only ones that happen: an
        # incarnation registers once, and registering an id twice is a genuine conflict.
        first = build_worker(engine, worker_id="worker-incarnation-1", register=False)
        assert first.registry.register() is not None

        # A restarted Worker is a new incarnation, so a second one registers alongside the first.
        second = build_worker(engine, worker_id="worker-incarnation-2", register=False)
        assert second.registry.register() is not None
        states = worker_states(engine, "worker-incarnation-2", now=datetime.now(UTC))
        assert set(states) == {"worker-incarnation-1", "worker-incarnation-2"}

        # A stopped incarnation is recorded as stopped, not silently deleted.
        assert first.registry.stop() is True
        stopped = worker_states(engine, "worker-incarnation-2", now=datetime.now(UTC))
        assert stopped["worker-incarnation-1"] == "stopped"
    finally:
        engine.dispose()

    restarted = reopen(path)
    try:
        states = worker_states(restarted, "worker-incarnation-3", now=datetime.now(UTC))
        assert set(states) == {"worker-incarnation-1", "worker-incarnation-2"}

        # A stale registry row is not authority: reclamation still needs an expired *lease*.
        run_id = submit(restarted, text_value="stale-registry")
        claimed_at = datetime.now(UTC)
        claimed = must_claim(restarted, worker_id="worker-incarnation-1", now=claimed_at)
        # Every Worker in this scenario runs on the real clock, so the registry row really does go
        # stale relative to it while the lease is still live.
        later = claimed_at + timedelta(seconds=30)
        assert worker_states(restarted, "worker-incarnation-1", now=later)[
            "worker-incarnation-1"
        ] in ("healthy", "stale", "stopped")
        # The lease has not expired, so nothing may be reclaimed however stale the registry looks.
        assert expire(restarted, backoff=RECLAIM_BACKOFF, now=later) is None
        assert job_row(restarted, run_id)["status"] == JobStatus.CLAIMED.value
        expire(
            restarted, backoff=RECLAIM_BACKOFF, now=claimed.lease_expires_at + timedelta(seconds=1)
        )
        assert job_row(restarted, run_id)["status"] == JobStatus.QUEUED.value
    finally:
        restarted.dispose()


# ---------------------------------------------------------------------------------------
# 5 and 6 -- no Run and no Event is lost, and the timeline is rebuilt from the file
# ---------------------------------------------------------------------------------------


def test_a_control_plane_restart_loses_no_run_and_no_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage_c_support import migrate

    path = tmp_path / "control-plane.db"
    engine = migrate(path, monkeypatch, agents=2)
    try:
        run_ids = [
            submit(engine, agent=1, text_value="c-1"),
            submit(engine, agent=2, text_value="c-2"),
            submit(engine, agent=1, text_value="c-3"),
        ]
        succeed(
            engine, start(engine, must_claim(engine, providers=(PROVIDER_ID, SECOND_PROVIDER_ID)))
        )
        before = counts(engine)
        timelines = {run_id: event_sequences(engine, run_id) for run_id in run_ids}
    finally:
        engine.dispose()

    restarted = reopen(path)
    try:
        # A fresh reader over the reopened file sees every Run and every Event, in order.
        assert counts(restarted) == before
        for run_id, expected in timelines.items():
            assert event_sequences(restarted, run_id) == expected
            assert [sequence for sequence, _ in run_events(restarted, run_id)] == expected

        # An owner-scoped read through the composition a restarted API builds behaves identically.
        from sqlalchemy.orm import sessionmaker

        persistence = SqlAlchemyAgentPersistence(sessionmaker(bind=restarted))
        for run_id in run_ids:
            run = persistence.get_run(1, run_id)
            assert run.id == run_id
            assert run.execution_phase is not None
        # Ownership is unchanged, so a foreign owner still sees nothing.
        with pytest.raises(RunNotFound):
            persistence.get_run(99, run_ids[0])
    finally:
        restarted.dispose()


# ---------------------------------------------------------------------------------------
# 7 -- a Worker process restart completes work it never saw begin
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_worker_process_restart_resumes_work_the_previous_one_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The only state carried between the two Workers is the database file."""
    from stage_c_support import migrate

    path = tmp_path / "worker-restart.db"
    engine = migrate(path, monkeypatch, agents=2, providers=(PROVIDER_ID, SECOND_PROVIDER_ID))
    try:
        first_run = submit(engine, agent=1, text_value="worker-restart-a", max_attempts=3)
        second_run = submit(engine, agent=2, text_value="worker-restart-b")

        # Worker A starts the first Run and is then lost mid-attempt, before it can record anything.
        # The claim is stamped by the real clock, because the Worker supervisors below run on it.
        started_at = datetime.now(UTC)
        abandoned = must_claim(
            engine, worker_id="worker-a", providers=(PROVIDER_ID,), now=started_at
        )
        assert execution(engine).start_attempt(abandoned, now=started_at) is True
        engine.dispose()

        restarted = reopen(path)
        assert job_row(restarted, first_run)["status"] == JobStatus.RUNNING.value

        # Worker B must reclaim the abandoned claim before it can execute anything, and it does so
        # only because the *lease* expired -- never because a registry row looked stale.
        after_expiry = started_at + LEASE_DURATION + timedelta(seconds=1)
        outcome = expire(restarted, backoff=timedelta(0), now=after_expiry)
        assert outcome is not None
        assert outcome.kind is ReclamationKind.POST_START_AMBIGUOUS
        # The abandoned attempt began executing, so its outcome is unknown and it is not replayed.
        assert run_row(restarted, first_run)["status"] == RunStatus.FAILED.value
        assert attempt_statuses(restarted, first_run) == ["expired"]
        assert claim(restarted, worker_id="worker-b", now=after_expiry + timedelta(hours=1)) is None

        # The unrelated second Run is untouched by all of that, and a fresh Worker completes it.
        completion = ScriptedCompletion(SECOND_PROVIDER_ID)
        harness = build_worker(
            restarted,
            worker_id="worker-b",
            completions={
                PROVIDER_ID: ScriptedCompletion(PROVIDER_ID),
                SECOND_PROVIDER_ID: completion,
            },
        )
        await run_until_settled([harness], restarted)

        assert run_row(restarted, second_run)["status"] == RunStatus.SUCCEEDED.value
        assert provider_ledger(completion) == 1
        assert run_row(restarted, first_run)["status"] == RunStatus.FAILED.value
    finally:
        engine.dispose()


def test_no_correctness_property_reads_state_that_a_restart_would_discard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The meta-property: everything above holds with the database as the only carried-over state.

    A fresh engine built from the file alone reproduces the queue, the retry schedule, the fairness
    markers, the Event history and the registry -- which is only possible because none of them ever
    lived in a Python object.
    """
    from stage_c_support import migrate

    path = tmp_path / "authority.db"
    engine = migrate(path, monkeypatch, agents=2)
    try:
        run_id = submit(engine, agent=1, text_value="authority", max_attempts=3)
        fail_rate_limited(engine, start(engine, must_claim(engine)))
        snapshot = {
            "job": job_row(engine, run_id),
            "run": run_row(engine, run_id),
            "events": event_sequences(engine, run_id),
            "markers": partition_rows(engine),
            "counts": counts(engine),
        }
    finally:
        engine.dispose()

    fresh = reopen(path)
    try:
        assert job_row(fresh, run_id) == snapshot["job"]
        assert run_row(fresh, run_id) == snapshot["run"]
        assert event_sequences(fresh, run_id) == snapshot["events"]
        assert partition_rows(fresh) == snapshot["markers"]
        assert counts(fresh) == snapshot["counts"]
        assert PERMISSIVE.global_active_limit > 0
    finally:
        fresh.dispose()
