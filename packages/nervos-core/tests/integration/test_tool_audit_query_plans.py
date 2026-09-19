"""D6 adds no index and no migration, and that rests on a measurement rather than a preference.

Two reads carry D6's cost: the public Run-timeline page (which now projects a nullable
`tool_invocation_id`) and the tool reconciliation the recovery and cancellation paths perform. Both
are planned here against a populated database, because "it uses the index" is a claim about what
SQLite does, not about what the query text looks like.

The measurement also pins the two things that would be easiest to get quietly wrong: neither read
may become a scan of a table that grows with every tool call the database has ever recorded, and the
timeline may not gain a join merely because it now names an invocation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from execution_support import migrate
from nervos_core.application.builtin_tools import (
    builtin_tool_specs,
    reconcile_builtin_definitions,
)
from nervos_core.application.tool_invocations import (
    InvocationRequest,
    StartOutcomeKind,
)
from nervos_core.application.tool_permissions import PermissionDecision
from nervos_core.domain.runs import TOOL_ENABLED_LIMITS
from nervos_core.infrastructure.database.agents import SqlAlchemyAgentPersistence
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobExecutionPersistence,
    SqlAlchemyJobPersistence,
    SqlAlchemyRunCancellationPersistence,
)
from nervos_core.infrastructure.database.models import (
    AgentToolGrantRecord,
    ToolDefinitionRecord,
    ToolInvocationRecord,
)
from nervos_core.infrastructure.database.tool_definitions import (
    SqlAlchemyToolDefinitionPersistence,
)
from nervos_core.infrastructure.database.tool_invocations import (
    SqlAlchemyToolInvocationPersistence,
)
from sqlalchemy import Engine, insert, select
from sqlalchemy.orm import sessionmaker

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
RUNS = 40
LEASE = timedelta(minutes=2)


class Plans:
    """Capture every statement one call site executes, then plan each one on the same database."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._captured: list[tuple[str, Any]] = []

    def __enter__(self) -> Plans:
        from sqlalchemy import event

        def before_cursor_execute(
            conn: object, cursor: object, statement: str, parameters: Any, *rest: object
        ) -> None:
            # The parameters are kept exactly as SQLAlchemy passed them -- a dict for an
            # executemany, a tuple for a positional statement -- so the plan is measured for the
            # statement that actually ran rather than for a rewritten one.
            self._captured.append((statement, parameters))

        self._listener = before_cursor_execute
        event.listen(self._engine, "before_cursor_execute", self._listener)
        return self

    def __exit__(self, *_exc: object) -> None:
        from sqlalchemy import event

        event.remove(self._engine, "before_cursor_execute", self._listener)

    def selects(self) -> list[tuple[str, str]]:
        """Return `(statement, plan)` for every captured statement."""
        raw = self._engine.raw_connection()
        try:
            cursor = raw.cursor()
            planned: list[tuple[str, str]] = []
            for statement, parameters in self._captured:
                if not statement.lstrip().upper().startswith(("SELECT", "UPDATE")):
                    continue
                cursor.execute(f"EXPLAIN QUERY PLAN {statement}", parameters)
                plan = " | ".join(str(row[3]) for row in cursor.fetchall())
                planned.append((statement, plan))
            return planned
        finally:
            raw.close()


@pytest.fixture
def populated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    """Enough Runs that a history-scanning read would show up as one.

    One of them is also left mid-Attempt with a *dispatched* tool invocation, because the
    reconciliation read only happens when there is something to reconcile -- measuring the plan of
    a query the code never issues would prove nothing.
    """
    engine = migrate(tmp_path / "tool-plans.db", monkeypatch, agents=1)
    definitions = SqlAlchemyToolDefinitionPersistence(engine)
    descriptors = reconcile_builtin_definitions(
        definitions, specs=builtin_tool_specs(clock=lambda: NOW), now=NOW
    )
    # The grant must exist *before* submission: a Run snapshots its grant cutoff at submit time,
    # so a grant created afterwards is invisible to it and the live check would refuse the call.
    with engine.begin() as connection:
        connection.execute(
            insert(AgentToolGrantRecord).values(
                agent_instance_id=1,
                tool_definition_id=descriptors[0].tool_definition_id,
                reviewed_fingerprint=connection.execute(
                    select(ToolDefinitionRecord.fingerprint).where(
                        ToolDefinitionRecord.id == descriptors[0].tool_definition_id
                    )
                ).scalar_one(),
                created_at=NOW,
            )
        )
    submission = SqlAlchemyJobPersistence(engine, max_pending=1000)
    for index in range(RUNS):
        submission.submit(
            owner_user_id=1,
            agent_instance_id=1,
            input_text=f"plan-{index}",
            limits=TOOL_ENABLED_LIMITS,
            now=NOW,
        )

    execution = SqlAlchemyJobExecutionPersistence(engine)
    handle = execution.claim_next(
        worker_id="worker-1",
        provider_ids=("anthropic",),
        max_active=4,
        now=NOW,
        lease_duration=LEASE,
    )
    assert handle is not None
    assert execution.start_attempt(handle, now=NOW) is True
    invocation = SqlAlchemyToolInvocationPersistence(engine).record_requested(
        claim=handle,
        request=InvocationRequest(
            run_id=handle.run_id,
            job_id=handle.job_id,
            attempt_id=handle.attempt_id,
            tool_sequence=1,
            tool_definition_id=descriptors[0].tool_definition_id,
            source_kind=descriptors[0].source_kind,
            source_id=descriptors[0].source_id,
            upstream_name=descriptors[0].upstream_name,
            model_name=descriptors[0].model_name,
            definition_fingerprint=descriptors[0].fingerprint,
            provider_call_id="call-1",
            permission_decision=PermissionDecision(allowed=True),
            arguments={"timezone": "UTC"},
        ),
        now=NOW,
    )
    assert invocation.invocation_id is not None
    assert (
        SqlAlchemyToolInvocationPersistence(engine)
        .mark_started(claim=handle, invocation_id=invocation.invocation_id, now=NOW)
        .kind
        is StartOutcomeKind.STARTED
    )
    return engine


def reads(engine: Engine) -> SqlAlchemyAgentPersistence:
    return SqlAlchemyAgentPersistence(sessionmaker(bind=engine))


def _dispatchable_run(engine: Engine) -> int:
    """The Run the fixture left mid-Attempt, which is the one with something to reconcile."""
    with engine.connect() as connection:
        return int(
            connection.execute(
                select(ToolInvocationRecord.run_id).where(ToolInvocationRecord.status == "started")
            ).scalar_one()
        )


def _reconciliations(plans: Plans) -> list[tuple[str, str]]:
    """The reconciliation statements only, identified by the Attempt that bounds them.

    Filtering on `attempt_id` is what distinguishes the engine's per-Attempt reconciliation from an
    unqualified count or existence read, which is precisely the shape that must never appear.
    """
    return [
        (statement, plan)
        for statement, plan in plans.selects()
        if "tool_invocations" in statement and "attempt_id" in statement
    ]


def test_the_timeline_page_is_still_one_bounded_index_range_seek(populated: Engine) -> None:
    """Projecting `tool_invocation_id` adds no join: it is a column of the row already read.

    The public projection deliberately exposes the link as an opaque identifier rather than
    enriching it into a tool name or a connection, and this is the measurement that keeps that
    decision honest -- enriching it would need a join to `tool_invocations` (and then onward to
    `tool_definitions`), which is exactly what a timeline read must not acquire.
    """
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
    # And it must not reach into the invocation table to render the new link.
    assert "tool_invocations" not in statement, statement


def test_the_cancellation_reconciliation_is_bounded_by_the_attempt_index(
    populated: Engine,
) -> None:
    """The reconciliation is an index seek over one Attempt, never a scan of the whole table.

    This is the read whose cost would otherwise grow with every tool call the database has ever
    recorded -- and it runs inside the recovery and cancellation transactions, so an unbounded plan
    here would be paid on the critical path of every Worker loss and every cancellation.
    """
    with Plans(populated) as plans:
        SqlAlchemyRunCancellationPersistence(populated).cancel_run(
            user_id=1, run_id=_dispatchable_run(populated), now=NOW + timedelta(hours=1)
        )

    reconciliations = _reconciliations(plans)
    assert reconciliations, "the cancellation path performed no reconciliation query"
    for statement, plan in reconciliations:
        # Every one is a bounded seek -- by primary key for the transition, by the Attempt index
        # for the scan that finds the calls -- and never a scan of the whole table.
        assert "SEARCH tool_invocations USING" in plan, (statement, plan)
        assert "attempt_id=?" in plan or "INTEGER PRIMARY KEY" in plan, (statement, plan)
        # Forty Runs and their calls exist; one Attempt's closeout must read only its own.
        assert "SCAN tool_invocations" not in plan, (statement, plan)


def test_the_recovery_reconciliation_uses_the_same_bounded_plan(populated: Engine) -> None:
    """The C3 path reads the same index as the C5 path: one mechanism, one cost."""
    with Plans(populated) as plans:
        SqlAlchemyJobExecutionPersistence(populated).reclaim_next_expired_claim(
            now=NOW + timedelta(days=1), backoff=timedelta(seconds=5)
        )

    for statement, plan in _reconciliations(plans):
        assert "SEARCH tool_invocations USING" in plan, (statement, plan)
        assert "attempt_id=?" in plan or "INTEGER PRIMARY KEY" in plan, (statement, plan)
        assert "SCAN tool_invocations" not in plan, (statement, plan)
