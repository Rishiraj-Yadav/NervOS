"""C2 atomic pending-capacity admission tests."""

from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path

import pytest
from execution_support import NOW, counts, job_id_of, job_row, migrate
from nervos_core.application.errors import QueueCapacityExceeded
from nervos_core.domain.jobs import JobStatus
from nervos_core.domain.runs import STAGE_B_LIMITS
from nervos_core.infrastructure.database import create_sqlite_engine
from nervos_core.infrastructure.database.jobs import SqlAlchemyJobPersistence
from sqlalchemy import Engine, text

LIMITS = STAGE_B_LIMITS


def submit(engine: Engine, *, owner: int = 1, text_value: str = "hello", capacity: int = 1000):
    return SqlAlchemyJobPersistence(engine, max_pending=capacity, sleep=lambda _: None).submit(
        owner_user_id=owner,
        agent_instance_id=1,
        input_text=text_value,
        limits=LIMITS,
        now=NOW,
    )


def test_submission_returns_the_persisted_created_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "capacity.db", monkeypatch)
    try:
        run = submit(engine)
        assert run.status.value == "created"
        assert run.agent_key == "nervos.chat"
        assert run.model_provider == "anthropic"
        assert run.model_name == "opaque/model"
        assert counts(engine) == {"runs": 1, "jobs": 1, "job_attempts": 0, "run_events": 2}
    finally:
        engine.dispose()


def test_capacity_breach_writes_no_rows_and_consumes_no_identifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "capacity.db", monkeypatch)
    try:
        submit(engine, capacity=1)
        before = counts(engine)
        with pytest.raises(QueueCapacityExceeded):
            submit(engine, text_value="second", capacity=1)
        assert counts(engine) == before
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT max(id) FROM runs")) == 1
    finally:
        engine.dispose()


def test_capacity_counts_claimed_and_running_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "capacity.db", monkeypatch)
    try:
        run = submit(engine, capacity=1)
        job_id = job_id_of(engine, run.id)
        # C2 never claims on the submission path, so simulate the occupying state directly.
        with engine.begin() as connection:
            connection.execute(text("UPDATE jobs SET status='claimed' WHERE id=:j"), {"j": job_id})
        with pytest.raises(QueueCapacityExceeded):
            submit(engine, text_value="second", capacity=1)
    finally:
        engine.dispose()


def test_terminal_jobs_release_capacity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = migrate(tmp_path / "capacity.db", monkeypatch)
    try:
        run = submit(engine, capacity=1)
        job_id = job_id_of(engine, run.id)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE jobs SET status='failed', finished_at=:n, error_code='model_refused',"
                    " error_message='safe' WHERE id=:j"
                ),
                {"j": job_id, "n": NOW + timedelta(seconds=1)},
            )
        assert submit(engine, text_value="second", capacity=1).status.value == "created"
        assert job_row(engine, job_id)["status"] == JobStatus.FAILED.value
    finally:
        engine.dispose()


def test_concurrent_submissions_cannot_exceed_the_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two racing submissions at capacity 1 serialize on the write lock: exactly one wins."""
    database = tmp_path / "race.db"
    engine = migrate(database, monkeypatch)

    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt() -> None:
        local = create_sqlite_engine(database)
        try:
            submit(local, capacity=1)
            outcome = "accepted"
        except QueueCapacityExceeded:
            outcome = "rejected"
        finally:
            local.dispose()
        with lock:
            outcomes.append(outcome)

    try:
        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(outcomes) == ["accepted", "rejected"]
        assert counts(engine) == {"runs": 1, "jobs": 1, "job_attempts": 0, "run_events": 2}
    finally:
        engine.dispose()


def test_repeated_submissions_create_distinct_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "capacity.db", monkeypatch)
    try:
        first = submit(engine, text_value="one")
        second = submit(engine, text_value="two")
        assert first.id != second.id
        assert counts(engine)["runs"] == 2
    finally:
        engine.dispose()


def test_capacity_is_global_across_owners(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins the accepted C0 global-cap behaviour: one owner's backlog can refuse another's.

    Per-instance and per-owner fairness are deliberately deferred to C6, so this test records
    the limitation rather than leaving it to be discovered later.
    """
    engine = migrate(tmp_path / "capacity.db", monkeypatch)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users"
                    "(username,password_hash,role,is_active,created_at,updated_at) "
                    "VALUES('other',X'00','admin',1,:n,:n)"
                ),
                {"n": NOW},
            )
            connection.execute(
                text(
                    "INSERT INTO agent_instances"
                    "(owner_user_id,agent_key,agent_definition_version,display_name,enabled,"
                    "model_provider,model_name,created_at,updated_at) "
                    "VALUES(2,'nervos.chat','1','Chat two',1,'anthropic','opaque/model',:n,:n)"
                ),
                {"n": NOW},
            )
        submit(engine, capacity=1)
        with pytest.raises(QueueCapacityExceeded):
            SqlAlchemyJobPersistence(engine, max_pending=1, sleep=lambda _: None).submit(
                owner_user_id=2,
                agent_instance_id=2,
                input_text="theirs",
                limits=LIMITS,
                now=NOW,
            )
    finally:
        engine.dispose()
