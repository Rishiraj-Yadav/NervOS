"""C6 concurrency caps: global, per-Agent, per-provider, and how they compose.

Every cap is enforced inside the one claim transaction, so these tests can assert exact admit /
deny boundaries rather than approximate ones. Fairness is orthogonal here: each test uses a single
eligible partition per dimension unless it is deliberately testing a skip.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from execution_support import NOW, migrate
from nervos_core.application.job_execution import LEASE_DURATION, ClaimedAttempt
from nervos_core.application.model_completion import MODEL_RATE_LIMITED, safe_error_message
from nervos_core.application.queue_policy import QueuePolicy
from nervos_core.application.retry_policy import PRODUCTION_RETRY_POLICY
from nervos_core.domain.jobs import RetryDisposition
from nervos_core.domain.runs import STAGE_B_LIMITS, ModelUsage
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobExecutionPersistence,
    SqlAlchemyJobPersistence,
    SqlAlchemyRunCancellationPersistence,
)
from sqlalchemy import Engine, text

ANTHROPIC = ("anthropic",)
OPENAI = ("openai",)
BOTH = ("anthropic", "openai")

# Wide everywhere except the one dimension a test is exercising.
WIDE = 64


def policy_for(
    *, global_limit: int = WIDE, agent_limit: int = WIDE, provider_limit: int = WIDE
) -> QueuePolicy:
    return QueuePolicy(
        global_active_limit=global_limit,
        per_agent_active_limit=agent_limit,
        per_provider_active_limit=provider_limit,
    )


def submit(engine: Engine, *, agent: int, text_value: str) -> int:
    return (
        SqlAlchemyJobPersistence(engine, max_pending=1000)
        .submit(
            owner_user_id=1,
            agent_instance_id=agent,
            input_text=text_value,
            limits=STAGE_B_LIMITS,
            now=NOW,
        )
        .id
    )


def execution(engine: Engine, policy: QueuePolicy):
    return SqlAlchemyJobExecutionPersistence(engine, policy=policy, sleep=lambda _: None)


def claim(
    engine: Engine,
    policy: QueuePolicy,
    *,
    worker_id: str = "worker-1",
    providers: tuple[str, ...] = BOTH,
    max_active: int = WIDE,
    now: datetime = NOW,
) -> ClaimedAttempt | None:
    return execution(engine, policy).claim_next(
        worker_id=worker_id,
        provider_ids=providers,
        max_active=max_active,
        now=now,
        lease_duration=LEASE_DURATION,
    )


def started(
    engine: Engine, policy: QueuePolicy, claimed: ClaimedAttempt, *, now: datetime = NOW
) -> ClaimedAttempt:
    assert execution(engine, policy).start_attempt(claimed, now=now) is True
    return claimed


def submit_many(engine: Engine, *, agent: int, count: int, prefix: str) -> list[int]:
    return [submit(engine, agent=agent, text_value=f"{prefix}-{index}") for index in range(count)]


# ---------------------------------------------------------------------------------------
# Each dimension independently
# ---------------------------------------------------------------------------------------


def test_the_global_cap_admits_exactly_n_live_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "global-cap.db", monkeypatch, agents=1)
    try:
        submit_many(engine, agent=1, count=4, prefix="job")
        capped = policy_for(global_limit=2)

        assert claim(engine, capped) is not None
        assert claim(engine, capped) is not None
        assert claim(engine, capped) is None
    finally:
        engine.dispose()


def test_the_per_agent_cap_limits_one_agent_without_limiting_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "agent-cap.db", monkeypatch, agents=2)
    try:
        submit_many(engine, agent=1, count=2, prefix="a1")
        submit_many(engine, agent=2, count=2, prefix="a2")
        capped = policy_for(agent_limit=1)

        first = claim(engine, capped)
        assert first is not None
        # Agent 2 is bounded by its own independent budget, and its turn has come.
        second = claim(engine, capped, now=NOW + timedelta(seconds=1))
        assert second is not None
        assert second.run_id != first.run_id
        # Both Instances are now at their own cap, so nothing further may claim.
        assert claim(engine, capped, now=NOW + timedelta(seconds=2)) is None
    finally:
        engine.dispose()


def test_the_per_provider_cap_limits_one_provider_without_limiting_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "provider-cap.db", monkeypatch, agents=2, providers=BOTH)
    try:
        # Agent 1 -> anthropic, Agent 2 -> openai, so the two partitions sit on distinct providers.
        submit_many(engine, agent=1, count=2, prefix="a1")
        submit_many(engine, agent=2, count=2, prefix="a2")
        capped = policy_for(provider_limit=1)

        assert claim(engine, capped) is not None  # anthropic
        assert claim(engine, capped, now=NOW + timedelta(seconds=1)) is not None  # openai
        assert claim(engine, capped, now=NOW + timedelta(seconds=2)) is None
    finally:
        engine.dispose()


def test_the_per_provider_cap_counts_a_provider_across_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The provider bound is per provider value, not per Agent: two Agents share one budget."""
    engine = migrate(tmp_path / "provider-shared.db", monkeypatch, agents=2)
    try:
        # Both Instances default to anthropic, so they contend for the same provider budget.
        submit_many(engine, agent=1, count=2, prefix="a1")
        submit_many(engine, agent=2, count=2, prefix="a2")
        capped = policy_for(provider_limit=1)

        assert claim(engine, capped) is not None
        assert claim(engine, capped, now=NOW + timedelta(seconds=1)) is None
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# Composition: the intersection of every cap
# ---------------------------------------------------------------------------------------


def test_global_room_is_not_enough_when_the_agent_is_at_its_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "intersect-agent.db", monkeypatch, agents=1)
    try:
        submit_many(engine, agent=1, count=3, prefix="job")
        capped = policy_for(global_limit=8, agent_limit=1)

        assert claim(engine, capped) is not None
        assert claim(engine, capped) is None
    finally:
        engine.dispose()


def test_global_room_is_not_enough_when_the_provider_is_at_its_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "intersect-provider.db", monkeypatch, agents=1)
    try:
        submit_many(engine, agent=1, count=3, prefix="job")
        capped = policy_for(global_limit=8, provider_limit=1)

        assert claim(engine, capped) is not None
        assert claim(engine, capped) is None
    finally:
        engine.dispose()


def test_agent_and_provider_room_are_not_enough_when_the_global_cap_is_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "intersect-global.db", monkeypatch, agents=2, providers=BOTH)
    try:
        submit_many(engine, agent=1, count=2, prefix="a1")
        submit_many(engine, agent=2, count=2, prefix="a2")
        capped = policy_for(global_limit=1)

        assert claim(engine, capped) is not None
        assert claim(engine, capped, now=NOW + timedelta(seconds=1)) is None
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# The Worker's own budget, and policy authority
# ---------------------------------------------------------------------------------------


def test_a_workers_own_budget_may_tighten_the_global_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "local-tighten.db", monkeypatch, agents=1)
    try:
        submit_many(engine, agent=1, count=3, prefix="job")
        wide = policy_for(global_limit=4)

        assert claim(engine, wide, max_active=1) is not None
        assert claim(engine, wide, max_active=1, now=NOW + timedelta(seconds=1)) is None
    finally:
        engine.dispose()


def test_a_workers_own_budget_cannot_raise_the_authoritative_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The property that makes the shared policy constants worth their rigidity.

    Each claim compares a database-wide count against its limit, so a Worker holding a *larger*
    number would let the fleet run above the intended bound. The authoritative limit is the
    ceiling, and no local budget can lift it.
    """
    engine = migrate(tmp_path / "local-raise.db", monkeypatch, agents=1)
    try:
        submit_many(engine, agent=1, count=3, prefix="job")
        authoritative = policy_for(global_limit=1)

        assert claim(engine, authoritative, max_active=WIDE) is not None
        assert claim(engine, authoritative, max_active=WIDE, now=NOW + timedelta(seconds=1)) is None
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# Release: every path that gives a slot back
# ---------------------------------------------------------------------------------------


def test_a_terminal_success_releases_its_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "release-success.db", monkeypatch, agents=1)
    try:
        submit_many(engine, agent=1, count=2, prefix="job")
        capped = policy_for(global_limit=1)
        held = claim(engine, capped)
        assert held is not None
        assert claim(engine, capped) is None

        started(engine, capped, held)
        assert execution(engine, capped).succeed(
            held,
            output_text="done",
            finish_reason="stop",
            usage=ModelUsage(1, 1, 2),
            elapsed_ms=5,
            now=NOW + timedelta(seconds=1),
        )

        assert claim(engine, capped, now=NOW + timedelta(seconds=1)) is not None
    finally:
        engine.dispose()


def test_a_cancellation_releases_its_slot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = migrate(tmp_path / "release-cancel.db", monkeypatch, agents=1)
    try:
        run_ids = submit_many(engine, agent=1, count=2, prefix="job")
        capped = policy_for(global_limit=1)
        held = claim(engine, capped)
        assert held is not None and held.run_id == run_ids[0]
        assert claim(engine, capped) is None

        SqlAlchemyRunCancellationPersistence(engine, sleep=lambda _: None).cancel_run(
            user_id=1, run_id=held.run_id, now=NOW + timedelta(seconds=1)
        )

        assert claim(engine, capped, now=NOW + timedelta(seconds=1)) is not None
    finally:
        engine.dispose()


def test_a_retry_wait_job_consumes_no_concurrency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "release-retry.db", monkeypatch, agents=1)
    try:
        submit_many(engine, agent=1, count=2, prefix="job")
        capped = policy_for(global_limit=1)
        held = claim(engine, capped)
        assert held is not None
        started(engine, capped, held)
        execution(engine, capped).record_failure(
            held,
            error_code=MODEL_RATE_LIMITED,
            error_message=safe_error_message(MODEL_RATE_LIMITED),
            retry_disposition=RetryDisposition.SAFE_TO_RETRY,
            usage=ModelUsage(1, 1, 2),
            elapsed_ms=5,
            anchor_at=NOW,
            retry_policy=PRODUCTION_RETRY_POLICY,
            now=NOW,
        )

        # `retry_wait` holds no lease, so the slot is free even though the Job is not terminal.
        assert claim(engine, capped, now=NOW + timedelta(milliseconds=500)) is not None
    finally:
        engine.dispose()


def test_an_expired_lease_stops_consuming_concurrency_without_enabling_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Freeing an expired slot is safe because the expired Job is unreachable by a claim.

    An expired Job is still `claimed`, so `_CLAIMABLE_SOURCES` cannot select it: only C3
    reclamation can requeue it. The freed slot therefore goes to *other* work, never to a second
    execution of the same Job.
    """
    engine = migrate(tmp_path / "release-expired.db", monkeypatch, agents=1)
    try:
        run_ids = submit_many(engine, agent=1, count=2, prefix="job")
        capped = policy_for(global_limit=1)
        held = claim(engine, capped)
        assert held is not None and held.run_id == run_ids[0]
        assert claim(engine, capped) is None

        after_expiry = NOW + LEASE_DURATION + timedelta(seconds=1)
        reclaimed = claim(engine, capped, now=after_expiry)

        assert reclaimed is not None
        assert reclaimed.job_id != held.job_id, "an expired Job must never be re-claimed directly"
        with engine.connect() as connection:
            expired_status = connection.scalar(
                text("SELECT status FROM jobs WHERE id=:j"), {"j": held.job_id}
            )
        assert expired_status == "claimed"
    finally:
        engine.dispose()
