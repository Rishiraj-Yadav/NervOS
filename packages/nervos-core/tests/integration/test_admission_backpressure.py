"""C6 admission backpressure: global, per-Agent, and per-provider pending bounds.

Admission is a control-plane concept and is deliberately separate from execution concurrency: a
full execution budget still admits work, and only a full *pending* budget refuses it. Every
dimension is counted inside the one submission transaction, so a rejection can never leave a
partially created Run behind.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path

import pytest
from execution_support import NOW, counts, migrate
from nervos_core.application.errors import QueueCapacityExceeded
from nervos_core.application.job_execution import LEASE_DURATION
from nervos_core.application.model_completion import MODEL_RATE_LIMITED, safe_error_message
from nervos_core.application.retry_policy import PRODUCTION_RETRY_POLICY
from nervos_core.domain.jobs import RetryDisposition
from nervos_core.domain.runs import STAGE_B_LIMITS, ModelUsage, Run
from nervos_core.infrastructure.database import create_sqlite_engine
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobExecutionPersistence,
    SqlAlchemyJobPersistence,
    SqlAlchemyRunCancellationPersistence,
)
from sqlalchemy import Engine

ANTHROPIC = ("anthropic",)
BOTH = ("anthropic", "openai")


def admission(
    engine: Engine,
    *,
    max_pending: int = 1000,
    per_agent: int | None = None,
    per_provider: int | None = None,
) -> SqlAlchemyJobPersistence:
    return SqlAlchemyJobPersistence(
        engine,
        max_pending=max_pending,
        max_pending_per_agent=per_agent,
        max_pending_per_provider=per_provider,
        sleep=lambda _: None,
    )


def attempt_submit(engine: Engine, *, agent: int, text_value: str, **kwargs: int) -> Run:
    return admission(engine, **kwargs).submit(  # type: ignore[arg-type]
        owner_user_id=1,
        agent_instance_id=agent,
        input_text=text_value,
        limits=STAGE_B_LIMITS,
        now=NOW,
    )


def execution(engine: Engine) -> SqlAlchemyJobExecutionPersistence:
    return SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None)


# ---------------------------------------------------------------------------------------
# Each dimension independently
# ---------------------------------------------------------------------------------------


def test_the_global_pending_cap_still_refuses_the_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "pending-global.db", monkeypatch, agents=1)
    try:
        attempt_submit(engine, agent=1, text_value="first", max_pending=1)
        with pytest.raises(QueueCapacityExceeded):
            attempt_submit(engine, agent=1, text_value="second", max_pending=1)
    finally:
        engine.dispose()


def test_the_per_agent_pending_cap_refuses_only_that_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "pending-agent.db", monkeypatch, agents=2)
    try:
        attempt_submit(engine, agent=1, text_value="a1-first", per_agent=1)
        with pytest.raises(QueueCapacityExceeded):
            attempt_submit(engine, agent=1, text_value="a1-second", per_agent=1)
        # A different Agent Instance has its own independent budget.
        attempt_submit(engine, agent=2, text_value="a2-first", per_agent=1)
    finally:
        engine.dispose()


def test_the_per_provider_pending_cap_refuses_only_that_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "pending-provider.db", monkeypatch, agents=2, providers=BOTH)
    try:
        attempt_submit(engine, agent=1, text_value="anthropic-first", per_provider=1)
        with pytest.raises(QueueCapacityExceeded):
            attempt_submit(engine, agent=1, text_value="anthropic-second", per_provider=1)
        # Agent 2 sits on openai, which is a separate provider budget.
        attempt_submit(engine, agent=2, text_value="openai-first", per_provider=1)
    finally:
        engine.dispose()


def test_the_per_provider_pending_cap_counts_across_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pending provider bound is per provider value, not per Agent."""
    engine = migrate(tmp_path / "pending-provider-shared.db", monkeypatch, agents=2)
    try:
        # Both Instances default to anthropic, so they share one provider budget.
        attempt_submit(engine, agent=1, text_value="a1", per_provider=1)
        with pytest.raises(QueueCapacityExceeded):
            attempt_submit(engine, agent=2, text_value="a2", per_provider=1)
    finally:
        engine.dispose()


def test_a_per_dimension_default_does_not_refuse_below_the_global_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the dimensions left unset they resolve to the global bound, so C6 admits exactly
    what C2 admitted and only an operator who lowers a dimension sees it bind."""
    engine = migrate(tmp_path / "pending-defaults.db", monkeypatch, agents=1)
    try:
        for index in range(3):
            attempt_submit(engine, agent=1, text_value=f"same-agent-{index}", max_pending=3)
        with pytest.raises(QueueCapacityExceeded):
            attempt_submit(engine, agent=1, text_value="overflow", max_pending=3)
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# A rejection must be total
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("dimension", ["global", "agent", "provider"])
def test_a_rejected_admission_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dimension: str
) -> None:
    engine = migrate(tmp_path / f"pending-atomic-{dimension}.db", monkeypatch, agents=1)
    try:
        kwargs = {
            "global": {"max_pending": 1},
            "agent": {"per_agent": 1},
            "provider": {"per_provider": 1},
        }[dimension]
        attempt_submit(engine, agent=1, text_value="accepted", **kwargs)
        before = counts(engine)

        with pytest.raises(QueueCapacityExceeded):
            attempt_submit(engine, agent=1, text_value="refused", **kwargs)

        assert counts(engine) == before
        assert counts(engine)["runs"] == 1
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------------------


def test_a_terminal_run_frees_pending_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "pending-terminal.db", monkeypatch, agents=1)
    try:
        run = attempt_submit(engine, agent=1, text_value="completes", per_agent=1)
        with pytest.raises(QueueCapacityExceeded):
            attempt_submit(engine, agent=1, text_value="blocked", per_agent=1)

        claimed = execution(engine).claim_next(
            worker_id="worker-1",
            provider_ids=ANTHROPIC,
            max_active=64,
            now=NOW,
            lease_duration=LEASE_DURATION,
        )
        assert claimed is not None
        assert execution(engine).start_attempt(claimed, now=NOW) is True
        assert execution(engine).succeed(
            claimed,
            output_text="done",
            finish_reason="stop",
            usage=ModelUsage(1, 1, 2),
            elapsed_ms=5,
            now=NOW,
        )

        attempt_submit(engine, agent=1, text_value="now-admitted", per_agent=1)
        assert run.id == claimed.run_id
    finally:
        engine.dispose()


def test_a_cancelled_run_frees_pending_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "pending-cancelled.db", monkeypatch, agents=1)
    try:
        run = attempt_submit(engine, agent=1, text_value="gets-cancelled", per_agent=1)
        SqlAlchemyRunCancellationPersistence(engine, sleep=lambda _: None).cancel_run(
            user_id=1, run_id=run.id, now=NOW + timedelta(seconds=1)
        )

        attempt_submit(engine, agent=1, text_value="admitted-after", per_agent=1)
    finally:
        engine.dispose()


def test_a_retry_wait_job_stays_exactly_one_pending_obligation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C4's durable retry must not double-count the same obligation against admission."""
    engine = migrate(tmp_path / "pending-retry.db", monkeypatch, agents=1)
    try:
        run = attempt_submit(engine, agent=1, text_value="will-retry", per_agent=1)
        claimed = execution(engine).claim_next(
            worker_id="worker-1",
            provider_ids=ANTHROPIC,
            max_active=64,
            now=NOW,
            lease_duration=LEASE_DURATION,
        )
        assert claimed is not None
        assert execution(engine).start_attempt(claimed, now=NOW) is True
        execution(engine).record_failure(
            claimed,
            error_code=MODEL_RATE_LIMITED,
            error_message=safe_error_message(MODEL_RATE_LIMITED),
            retry_disposition=RetryDisposition.SAFE_TO_RETRY,
            usage=ModelUsage(1, 1, 2),
            elapsed_ms=5,
            anchor_at=NOW,
            retry_policy=PRODUCTION_RETRY_POLICY,
            now=NOW,
        )

        # Still exactly one obligation, so the dimension is still full.
        assert counts(engine)["jobs"] == 1
        assert counts(engine)["job_attempts"] == 1
        with pytest.raises(QueueCapacityExceeded):
            attempt_submit(engine, agent=1, text_value="still-blocked", per_agent=1)
        assert run.id == claimed.run_id
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# The last-slot race
# ---------------------------------------------------------------------------------------


def test_concurrent_submissions_at_the_last_slot_admit_exactly_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counting and inserting share one `BEGIN IMMEDIATE`, so the losers write nothing."""
    engine = migrate(tmp_path / "pending-race.db", monkeypatch, agents=1)
    try:
        outcomes: list[str] = []
        barrier = threading.Barrier(2)

        def attempt(index: int) -> None:
            local = create_sqlite_engine(tmp_path / "pending-race.db")
            try:
                barrier.wait(timeout=10)
                try:
                    admission(local, max_pending=1).submit(
                        owner_user_id=1,
                        agent_instance_id=1,
                        input_text=f"racer-{index}",
                        limits=STAGE_B_LIMITS,
                        now=NOW,
                    )
                except QueueCapacityExceeded:
                    outcomes.append("refused")
                else:
                    outcomes.append("accepted")
            finally:
                local.dispose()

        threads = [threading.Thread(target=attempt, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert sorted(outcomes) == ["accepted", "refused"]
        # Exactly one Run exists, and the refused submission left nothing behind.
        assert counts(engine)["runs"] == 1
        assert counts(engine)["jobs"] == 1
    finally:
        engine.dispose()
