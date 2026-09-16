"""C7 query plans: what the timeline read actually costs, and what it must never cost.

C7 adds no index and no migration. That decision rests on a measurement rather than a preference:
`UNIQUE(run_id, sequence)` already materialises in SQLite as an implicit index, and the pagination
predicate is a bounded range seek on both of its columns. These tests capture the statements the
persistence really issues, replay each through `EXPLAIN QUERY PLAN`, and assert that shape.

They deliberately assert the *shape* of the plan rather than its exact string, so a SQLite version
that phrases a seek differently does not fail the suite for the wrong reason -- but a timeline that
started scanning the table, or that sorted through a temporary b-tree, does.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from execution_support import NOW, migrate
from nervos_core.domain.runs import STAGE_B_LIMITS
from nervos_core.infrastructure.database.agents import SqlAlchemyAgentPersistence
from nervos_core.infrastructure.database.jobs import SqlAlchemyJobPersistence
from sqlalchemy import Engine, event
from sqlalchemy.orm import sessionmaker

RUNS = 40
EVENTS_PER_RUN = 2


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
                plan = " | ".join(str(row[3]) for row in cursor.fetchall())
                planned.append((statement, plan))
            return planned
        finally:
            raw.close()


@pytest.fixture
def populated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    """Enough Runs and Events that a history-scanning query would show up as one."""
    engine = migrate(tmp_path / "event-plans.db", monkeypatch)
    submission = SqlAlchemyJobPersistence(engine, max_pending=1000)
    for index in range(RUNS):
        submission.submit(
            owner_user_id=1,
            agent_instance_id=1,
            input_text=f"plan-{index}",
            limits=STAGE_B_LIMITS,
            now=NOW,
        )
    return engine


def reads(engine: Engine) -> SqlAlchemyAgentPersistence:
    return SqlAlchemyAgentPersistence(sessionmaker(bind=engine))


def test_the_event_page_is_a_bounded_index_range_seek(
    populated: Engine,
) -> None:
    """Both columns of the predicate are served by the index the schema already maintains."""
    with Plans(populated) as plans:
        reads(populated).list_run_events(1, 0, 50)

    pages = [(statement, plan) for statement, plan in plans.selects() if "run_events" in plan]
    assert len(pages) == 1, pages
    statement, plan = pages[0]

    assert "SEARCH run_events USING" in plan, (statement, plan)
    assert "run_id=?" in plan and "sequence>?" in plan, (statement, plan)
    # The index supplies ascending sequence, so no sort step is needed to satisfy ORDER BY.
    assert "TEMP B-TREE" not in plan, (statement, plan)
    # 40 Runs exist; a timeline for one of them must not read the others.
    assert "SCAN run_events" not in plan, (statement, plan)


def test_the_event_page_plan_is_identical_at_any_cursor(populated: Engine) -> None:
    """A deep cursor costs the same as the first page: there is no offset to walk."""
    with Plans(populated) as plans:
        reads(populated).list_run_events(1, 0, 50)
        reads(populated).list_run_events(1, 1, 50)
        reads(populated).list_run_events(RUNS, 0, 50)

    plans_by_plan = {plan for _, plan in plans.selects() if "run_events" in plan}
    assert len(plans_by_plan) == 1, plans_by_plan


def test_the_timeline_cost_scales_with_one_runs_history(populated: Engine) -> None:
    """The predicate is scoped by `run_id`, so another Run's Events are never reached."""
    with Plans(populated) as plans:
        reads(populated).list_run_events(RUNS, 0, 50)

    for statement, plan in plans.selects():
        if "run_events" not in plan:
            continue
        assert "run_id=?" in plan, (statement, plan)


def test_a_run_read_joins_its_one_job_through_the_unique_index(populated: Engine) -> None:
    """The derived phase costs one extra index lookup, not a table scan and not a second query."""
    with Plans(populated) as plans:
        reads(populated).get_run(1, 1)

    queries = plans.selects()
    assert len(queries) == 1, "the phase must come from the same committed read as the Run"
    statement, plan = queries[0]

    assert "SEARCH runs USING INTEGER PRIMARY KEY" in plan, (statement, plan)
    assert "SEARCH jobs USING" in plan and "run_id=?" in plan, (statement, plan)
    assert "SCAN runs" not in plan and "SCAN jobs" not in plan, (statement, plan)


def test_a_run_page_joins_its_phase_without_reaching_a_second_table(populated: Engine) -> None:
    with Plans(populated) as plans:
        reads(populated).list_runs(1, 1, 20, None)

    queries = plans.selects()
    assert len(queries) == 1, queries
    statement, plan = queries[0]

    # A larger page costs more index seeks, never a second round trip per Run.
    assert "SCAN runs" not in plan and "SCAN jobs" not in plan, (statement, plan)
    assert "SEARCH jobs USING" in plan, (statement, plan)


def test_no_c7_read_scans_a_base_table(populated: Engine) -> None:
    with Plans(populated) as plans:
        reads(populated).list_run_events(1, 0, 50)
        reads(populated).get_run(1, 1)
        reads(populated).list_runs(1, 1, 20, None)

    queries = plans.selects()
    assert queries, "the paths under test must issue at least one SELECT"
    scanned = [
        (statement, plan)
        for statement, plan in queries
        if any(
            f"SCAN {table}" in plan for table in ("run_events", "runs", "jobs", "agent_instances")
        )
    ]
    assert scanned == [], scanned
