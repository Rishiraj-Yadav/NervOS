"""C2 one-winner claim, capability filtering, and global active-cap tests."""

from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path

import pytest
from execution_support import NOW, attempt_row, counts, event_types, job_row, migrate
from nervos_core.application.job_execution import LEASE_DURATION
from nervos_core.domain.jobs import AttemptStatus, JobStatus
from nervos_core.domain.runs import STAGE_B_LIMITS
from nervos_core.infrastructure.database import create_sqlite_engine
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobExecutionPersistence,
    SqlAlchemyJobPersistence,
)
from sqlalchemy import Engine, text

ANTHROPIC = ("anthropic",)
BOTH = ("anthropic", "openai")


def submit(engine: Engine, *, text_value: str = "hello", capacity: int = 1000):
    return SqlAlchemyJobPersistence(engine, max_pending=capacity).submit(
        owner_user_id=1,
        agent_instance_id=1,
        input_text=text_value,
        limits=STAGE_B_LIMITS,
        now=NOW,
    )


def execution(engine: Engine) -> SqlAlchemyJobExecutionPersistence:
    return SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None)


def claim(
    engine: Engine,
    *,
    worker_id: str = "worker-1",
    providers: tuple[str, ...] = ANTHROPIC,
    max_active: int = 4,
):
    return execution(engine).claim_next(
        worker_id=worker_id,
        provider_ids=providers,
        max_active=max_active,
        now=NOW,
        lease_duration=LEASE_DURATION,
    )


def test_claim_creates_one_attempt_and_one_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "claim.db", monkeypatch)
    try:
        run = submit(engine)
        claimed = claim(engine)
        assert claimed is not None
        assert claimed.run_id == run.id
        assert claimed.attempt_number == 1
        assert len(claimed.claim_token) == 32

        job = job_row(engine, claimed.job_id)
        assert job["status"] == JobStatus.CLAIMED.value
        assert job["attempt_count"] == 1
        assert job["claimed_by"] == "worker-1"
        assert job["lease_expires_at"] is not None

        attempt = attempt_row(engine, claimed.attempt_id)
        assert attempt["status"] == AttemptStatus.CLAIMED.value
        assert attempt["execution_started_at"] is None
        assert attempt["worker_id"] == "worker-1"

        assert event_types(engine, run.id) == ["run.created", "run.queued", "attempt.claimed"]
        assert counts(engine)["job_attempts"] == 1
    finally:
        engine.dispose()


def test_two_independent_claimers_produce_exactly_one_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "claim.db", monkeypatch)
    try:
        run = submit(engine)
        from nervos_core.application.job_execution import ClaimedAttempt

        results: list[ClaimedAttempt | None] = [None, None]
        barrier = threading.Barrier(2)

        def attempt(index: int) -> None:
            local = create_sqlite_engine(tmp_path / "claim.db")
            try:
                barrier.wait(timeout=10)
                results[index] = execution(local).claim_next(
                    worker_id=f"worker-{index}",
                    provider_ids=ANTHROPIC,
                    max_active=4,
                    now=NOW,
                    lease_duration=LEASE_DURATION,
                )
            finally:
                local.dispose()

        threads = [threading.Thread(target=attempt, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        winners = [result for result in results if result is not None]
        winner = winners[0]
        assert len(winners) == 1
        assert results.count(None) == 1
        # The loser created no Attempt and no Event, so the durable shape is exactly one claim.
        assert counts(engine)["job_attempts"] == 1
        assert event_types(engine, run.id).count("attempt.claimed") == 1
        assert job_row(engine, winner.job_id)["attempt_count"] == 1
    finally:
        engine.dispose()


def test_global_active_cap_is_enforced_across_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "cap.db", monkeypatch)
    try:
        for index in range(4):
            submit(engine, text_value=f"job {index}")
        first = claim(engine, worker_id="a", max_active=2)
        second = claim(engine, worker_id="b", max_active=2)
        assert first is not None and second is not None
        assert claim(engine, worker_id="c", max_active=2) is None
        assert counts(engine)["job_attempts"] == 2
    finally:
        engine.dispose()


def test_expired_lease_releases_active_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An expired claim stops occupying capacity *because* it has no write authority."""
    engine = migrate(tmp_path / "cap.db", monkeypatch)
    try:
        submit(engine, text_value="first")
        submit(engine, text_value="second")
        assert claim(engine, worker_id="a", max_active=1) is not None
        assert claim(engine, worker_id="b", max_active=1) is None

        later = NOW + LEASE_DURATION + timedelta(seconds=1)
        claimed = execution(engine).claim_next(
            worker_id="b",
            provider_ids=ANTHROPIC,
            max_active=1,
            now=later,
            lease_duration=LEASE_DURATION,
        )
        assert claimed is not None
        assert claimed.attempt_number == 1
    finally:
        engine.dispose()


def test_a_worker_without_the_provider_never_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capability absence is a queue state, not an execution outcome."""
    engine = migrate(tmp_path / "capability.db", monkeypatch)
    try:
        run = submit(engine)
        assert claim(engine, providers=("openai",)) is None
        job_id = int(
            engine.connect().scalar(text("SELECT id FROM jobs WHERE run_id=:r"), {"r": run.id})
        )
        job = job_row(engine, job_id)
        assert job["status"] == JobStatus.QUEUED.value
        assert job["attempt_count"] == 0
        assert counts(engine)["job_attempts"] == 0
        assert event_types(engine, run.id) == ["run.created", "run.queued"]
        # Another Worker that does have the capability still executes it normally.
        assert claim(engine, providers=BOTH) is not None
    finally:
        engine.dispose()


def test_zero_provider_worker_issues_no_query_and_claims_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "capability.db", monkeypatch)
    try:
        submit(engine)
        assert claim(engine, providers=()) is None
        assert counts(engine)["job_attempts"] == 0
    finally:
        engine.dispose()


def test_claim_skips_an_ineligible_older_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Selection is the oldest *eligible* Job, not global FIFO across providers."""
    engine = migrate(tmp_path / "capability.db", monkeypatch)
    try:
        # Re-point the first Run's snapshot at a provider this Worker cannot execute.
        first = submit(engine, text_value="older")
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE runs SET model_provider='openai' WHERE id=:r"), {"r": first.id}
            )
            connection.execute(
                text("UPDATE jobs SET model_provider='openai' WHERE run_id=:r"), {"r": first.id}
            )
        second = submit(engine, text_value="newer")
        claimed = claim(engine, providers=ANTHROPIC)
        assert claimed is not None
        assert claimed.run_id == second.id
    finally:
        engine.dispose()
