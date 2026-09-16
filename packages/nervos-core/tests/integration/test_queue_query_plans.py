"""C6 query plans: what the claim and admission statements actually cost.

These tests do not mirror the SQL by hand. They capture the statements the persistence really
issues, replay each one through SQLite's `EXPLAIN QUERY PLAN`, and assert the two properties that
matter: the load-bearing predicates are index-served rather than table-scanned, and **no claim
statement ever touches attempt history**. The second is the reason C6 stores a per-partition
marker at all: a fair ordering that scanned `job_attempts` would get slower every time the system
ran anything.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from execution_support import NOW, migrate
from nervos_core.application.job_execution import LEASE_DURATION
from nervos_core.application.queue_policy import QueuePolicy
from nervos_core.domain.runs import STAGE_B_LIMITS
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobExecutionPersistence,
    SqlAlchemyJobPersistence,
)
from sqlalchemy import Engine, event

ANTHROPIC = ("anthropic",)
WIDE = 64
PERMISSIVE = QueuePolicy(
    global_active_limit=WIDE, per_agent_active_limit=WIDE, per_provider_active_limit=WIDE
)


class Plans:
    """Every SELECT a code path issued, with its query plan."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._captured: list[tuple[str, Any]] = []

    def __enter__(self) -> Plans:
        event.listen(self._engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *_: object) -> None:
        event.remove(self._engine, "before_cursor_execute", self._record)

    def _record(
        self,
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: Any,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            self._captured.append((statement, parameters))

    def selects(self) -> list[tuple[str, str]]:
        """Return `(statement, plan)` for every captured SELECT."""
        raw = self._engine.raw_connection()
        try:
            cursor = raw.cursor()
            planned: list[tuple[str, str]] = []
            for statement, parameters in self._captured:
                cursor.execute(f"EXPLAIN QUERY PLAN {statement}", parameters)
                rows = cursor.fetchall()
                plan = " | ".join(str(row[3]) for row in rows)
                planned.append((statement, plan))
            return planned
        finally:
            raw.close()


def submit(engine: Engine, *, agent: int, count: int) -> None:
    persistence = SqlAlchemyJobPersistence(engine, max_pending=1000)
    for index in range(count):
        persistence.submit(
            owner_user_id=1,
            agent_instance_id=agent,
            input_text=f"plan-{agent}-{index}",
            limits=STAGE_B_LIMITS,
            now=NOW,
        )


def claim(engine: Engine) -> None:
    SqlAlchemyJobExecutionPersistence(engine, policy=PERMISSIVE, sleep=lambda _: None).claim_next(
        worker_id="worker-1",
        provider_ids=ANTHROPIC,
        max_active=WIDE,
        now=NOW,
        lease_duration=LEASE_DURATION,
    )


@pytest.fixture
def populated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    """A database with enough history that a history-scanning query would show up as such."""
    engine = migrate(tmp_path / "plans.db", monkeypatch, agents=2)
    submit(engine, agent=1, count=4)
    submit(engine, agent=2, count=4)
    for _ in range(4):
        claim(engine)
    return engine


def test_no_claim_or_admission_statement_scans_a_table(
    populated: Engine,
) -> None:
    """Every captured SELECT must reach its rows through an index.

    A `SCAN` of a *derived* relation is expected and bounded -- the window function materializes
    one small relation whose size is capped by the pending admission bound -- but no base table
    may ever be scanned.
    """
    queries: list[tuple[str, str]] = []
    with Plans(populated) as plans:
        claim(populated)
    queries.extend(plans.selects())
    with Plans(populated) as plans:
        submit(populated, agent=1, count=1)
    queries.extend(plans.selects())

    assert queries, "the paths under test must issue at least one SELECT"
    scanned = [
        (statement, plan)
        for statement, plan in queries
        if any(f"SCAN {table}" in plan for table in ("jobs", "job_attempts", "runs", "run_events"))
    ]
    assert scanned == [], f"a base table was scanned: {scanned}"


def test_no_claim_statement_touches_attempt_history(populated: Engine) -> None:
    """The fairness ordering must not read `job_attempts`, or its cost would grow with every
    Run the system ever executed. The per-partition marker exists precisely so it does not."""
    with Plans(populated) as plans:
        claim(populated)

    touching_history = [
        (statement, plan) for statement, plan in plans.selects() if "job_attempts" in plan
    ]
    assert touching_history == [], touching_history


def test_the_selection_reads_durable_partition_metadata_by_primary_key(
    populated: Engine,
) -> None:
    """The ordering fact comes from `queue_partitions`, reached by its Agent-Instance key."""
    with Plans(populated) as plans:
        claim(populated)

    selection = [
        (statement, plan) for statement, plan in plans.selects() if "queue_partitions" in plan
    ]
    assert selection, "the claim must consult the fairness metadata"
    for statement, plan in selection:
        assert "SEARCH queue_partitions USING INTEGER PRIMARY KEY" in plan, (statement, plan)


def test_the_active_count_is_lease_index_served(populated: Engine) -> None:
    """Concurrency is counted from live leases, so the lease-ordered index drives that count."""
    with Plans(populated) as plans:
        claim(populated)

    lease_served = [
        (statement, plan)
        for statement, plan in plans.selects()
        if "SELECT jobs.agent_instance_id" in statement
    ]
    assert lease_served, "the claim must count live leases"
    for statement, plan in lease_served:
        assert "ix_jobs_status_lease_expires_at_id" in plan, (statement, plan)


def test_the_candidate_selection_is_available_at_index_served(populated: Engine) -> None:
    """The bounded candidate set is found through the (status, available_at, id) index."""
    with Plans(populated) as plans:
        claim(populated)

    candidate = [
        (statement, plan)
        for statement, plan in plans.selects()
        if "ROW_NUMBER" in statement or "ranked" in statement
    ]
    assert candidate, "the claim must rank candidate Jobs"
    for statement, plan in candidate:
        assert "ix_jobs_status_available_at_id" in plan, (statement, plan)
