"""C2 transaction-failure classification, safe replay, and read-after-error reconciliation.

These tests pin the exact policy derived from the isolated SQLite validation spike: only a
busy/locked failure is provably-uncommitted and therefore replayable, and an unobserved
commit is resolved by *reading durable state*, never by blindly repeating the operation.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from execution_support import NOW, counts, event_types, migrate
from nervos_core.application.errors import PersistenceUnavailable
from nervos_core.application.job_execution import LEASE_DURATION
from nervos_core.domain.runs import STAGE_B_LIMITS
from nervos_core.infrastructure.database import jobs as jobs_module
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobExecutionPersistence,
    SqlAlchemyJobPersistence,
)
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError, SQLAlchemyError

BUSY = 5
IOERR = 10


def driver_error(message: str, code: int | None) -> SQLAlchemyError:
    """Build the SQLAlchemy wrapper a real driver failure would produce."""
    origin = sqlite3.OperationalError(message)
    if code is not None:
        origin.sqlite_errorcode = code
    return OperationalError("stmt", {}, origin)


class FlakySubmit(SqlAlchemyJobPersistence):
    """Raise on the first operation attempt, then delegate to production behaviour."""

    def __init__(self, engine: Engine, error: SQLAlchemyError, *, fail_times: int = 1) -> None:
        super().__init__(engine, max_pending=100, sleep=lambda _: None)
        self.error = error
        self.fail_times = fail_times
        self.attempts = 0

    def _submit_once(self, *args: Any, **kwargs: Any) -> Any:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise self.error
        return super()._submit_once(*args, **kwargs)  # pyright: ignore[reportPrivateUsage]


class FlakyClaim(SqlAlchemyJobExecutionPersistence):
    """Raise on the first claim attempt, then delegate to production behaviour."""

    def __init__(self, engine: Engine, error: SQLAlchemyError) -> None:
        super().__init__(engine, sleep=lambda _: None)
        self.error = error
        self.attempts = 0

    def _claim_once(self, *args: Any, **kwargs: Any) -> Any:
        self.attempts += 1
        if self.attempts == 1:
            raise self.error
        return super()._claim_once(*args, **kwargs)  # pyright: ignore[reportPrivateUsage]


class CommitThenFail:
    """Commit the operation for real, then report an unobserved failure to the caller."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def run(self, operation: Any, *, attempts: int = 3) -> Any:
        del attempts
        connection = self._engine.connect()
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            operation(connection)
            connection.commit()
        finally:
            connection.close()
        raise PersistenceUnavailable


def submit(persistence: SqlAlchemyJobPersistence, *, text_value: str = "hello"):
    return persistence.submit(
        owner_user_id=1,
        agent_instance_id=1,
        input_text=text_value,
        limits=STAGE_B_LIMITS,
        now=NOW,
    )


def test_a_real_sqlite_write_contention_is_classified_as_provably_uncommitted(
    tmp_path: Path,
) -> None:
    """The classifier reads the driver's own numeric code, not a matched error string."""
    path = tmp_path / "contention.db"
    holder = sqlite3.connect(str(path), isolation_level=None)
    holder.execute("CREATE TABLE t (id INTEGER)")
    holder.execute("BEGIN IMMEDIATE")
    try:
        other = sqlite3.connect(str(path), timeout=0.05)
        try:
            other.execute("PRAGMA busy_timeout=50")
            with pytest.raises(sqlite3.OperationalError) as info:
                other.execute("BEGIN IMMEDIATE")
        finally:
            other.close()
    finally:
        holder.close()

    assert getattr(info.value, "sqlite_errorcode", None) == BUSY
    classify = jobs_module._is_contention  # pyright: ignore[reportPrivateUsage]
    assert classify(OperationalError("BEGIN IMMEDIATE", {}, info.value))
    # A non-contention driver failure is never treated as replayable.
    assert not classify(driver_error("disk I/O error", IOERR))
    assert not classify(driver_error("some other failure", None))


def test_a_proven_contention_is_replayed_on_a_fresh_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "busy.db", monkeypatch)
    try:
        persistence = FlakySubmit(engine, driver_error("database is locked", BUSY))
        run = submit(persistence)
        assert persistence.attempts == 2
        assert run.id == 1
        # The replay produced exactly one Run/Job/event set: no duplicate, no lost write.
        assert counts(engine) == {"runs": 1, "jobs": 1, "job_attempts": 0, "run_events": 2}
    finally:
        engine.dispose()


def test_an_unproven_failure_is_never_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-contention failure could have committed, so it is reported, not repeated."""
    engine = migrate(tmp_path / "unknown.db", monkeypatch)
    try:
        persistence = FlakySubmit(engine, driver_error("disk I/O error", IOERR))
        with pytest.raises(PersistenceUnavailable):
            submit(persistence)
        assert persistence.attempts == 1
        assert counts(engine) == {"runs": 0, "jobs": 0, "job_attempts": 0, "run_events": 0}
    finally:
        engine.dispose()


def test_a_proven_contention_exhausting_the_budget_reports_a_persistence_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "exhausted.db", monkeypatch)
    try:
        persistence = FlakySubmit(engine, driver_error("database is locked", BUSY), fail_times=99)
        with pytest.raises(PersistenceUnavailable):
            submit(persistence)
        assert persistence.attempts == 3
        assert counts(engine)["runs"] == 0
    finally:
        engine.dispose()


def test_an_unobserved_submission_commit_is_reconciled_by_reading_durable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Run+Job+events state exists, so the submission is treated as committed — exactly once."""
    engine = migrate(tmp_path / "reconcile.db", monkeypatch)
    try:
        persistence = SqlAlchemyJobPersistence(engine, max_pending=100, sleep=lambda _: None)
        monkeypatch.setattr(persistence, "_runner", CommitThenFail(engine))
        run = submit(persistence)
        monkeypatch.undo()

        assert run.status.value == "created"
        assert counts(engine) == {"runs": 1, "jobs": 1, "job_attempts": 0, "run_events": 2}
        assert event_types(engine, run.id) == ["run.created", "run.queued"]
        # Repeated submissions still create distinct Runs; nothing was deduplicated away.
        assert submit(persistence).id != run.id
    finally:
        engine.dispose()


def test_an_uncertain_claim_commit_recovers_the_exact_committed_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "claim.db", monkeypatch)
    try:
        SqlAlchemyJobPersistence(engine, max_pending=100).submit(
            owner_user_id=1,
            agent_instance_id=1,
            input_text="hello",
            limits=STAGE_B_LIMITS,
            now=NOW,
        )
        execution = SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None)
        monkeypatch.setattr(execution, "_runner", CommitThenFail(engine))
        claimed = execution.claim_next(
            worker_id="worker-1",
            provider_ids=("anthropic",),
            max_active=4,
            now=NOW,
            lease_duration=LEASE_DURATION,
        )
        monkeypatch.undo()

        assert claimed is not None
        assert len(claimed.claim_token) == 32
        assert counts(engine)["job_attempts"] == 1
        assert event_types(engine, claimed.run_id) == [
            "run.created",
            "run.queued",
            "attempt.claimed",
        ]
    finally:
        engine.dispose()
