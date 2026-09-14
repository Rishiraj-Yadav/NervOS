"""C1 dormant durable submission persistence tests."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.runs import STAGE_B_LIMITS
from nervos_core.infrastructure.database import create_sqlite_engine
from nervos_core.infrastructure.database.jobs import (
    DurableSubmissionRejected,
    SqlAlchemyJobPersistence,
)
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[4]
NOW = datetime(2026, 9, 14, tzinfo=UTC)


def setup_db(path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NERVOS_DATABASE_PATH", str(path))
    cfg = Config(str(ROOT / "apps/api/alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "apps/api/alembic"))
    command.upgrade(cfg, "head")
    engine = create_sqlite_engine(path)
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO users"
                "(username,password_hash,role,is_active,created_at,updated_at) "
                "VALUES('owner',X'00','admin',1,:n,:n)"
            ),
            {"n": NOW},
        )
        c.execute(
            text(
                "INSERT INTO agent_instances"
                "(owner_user_id,agent_key,agent_definition_version,display_name,enabled,"
                "model_provider,model_name,created_at,updated_at) "
                "VALUES(1,'nervos.chat','1','Chat',1,'anthropic','model',:n,:n)"
            ),
            {"n": NOW},
        )
    return engine


def test_atomic_submission_snapshots_and_creates_initial_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = setup_db(tmp_path / "c1.db", monkeypatch)
    try:
        run_id, job_id = SqlAlchemyJobPersistence(engine).submit(
            owner_user_id=1, agent_instance_id=1, input_text="hello", limits=STAGE_B_LIMITS, now=NOW
        )
        with engine.connect() as c:
            assert c.scalar(text("SELECT count(*) FROM runs")) == 1
            assert c.scalar(text("SELECT count(*) FROM jobs")) == 1
            assert c.execute(
                text("SELECT event_type FROM run_events WHERE run_id=:r ORDER BY sequence"),
                {"r": run_id},
            ).scalars().all() == ["run.created", "run.queued"]
            assert c.scalar(text("SELECT run_id FROM jobs WHERE id=:j"), {"j": job_id}) == run_id
    finally:
        engine.dispose()


def test_submission_rejects_foreign_and_disabled_without_partial_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = setup_db(tmp_path / "reject.db", monkeypatch)
    persistence = SqlAlchemyJobPersistence(engine)
    try:
        for owner in (2,):
            with pytest.raises(DurableSubmissionRejected):
                persistence.submit(
                    owner_user_id=owner,
                    agent_instance_id=1,
                    input_text="hello",
                    limits=STAGE_B_LIMITS,
                    definition_id=AgentDefinitionId("nervos.chat", "1"),
                    now=NOW,
                )
        with engine.begin() as c:
            c.execute(text("UPDATE agent_instances SET enabled=0 WHERE id=1"))
        with pytest.raises(DurableSubmissionRejected):
            persistence.submit(
                owner_user_id=1,
                agent_instance_id=1,
                input_text="hello",
                limits=STAGE_B_LIMITS,
                definition_id=AgentDefinitionId("nervos.chat", "1"),
                now=NOW,
            )
        with engine.connect() as c:
            assert c.scalar(text("SELECT count(*) FROM runs")) == 0
    finally:
        engine.dispose()


def test_append_event_sequences_independent_writers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = setup_db(tmp_path / "events.db", monkeypatch)
    try:
        run_id, job_id = SqlAlchemyJobPersistence(engine).submit(
            owner_user_id=1, agent_instance_id=1, input_text="hello", limits=STAGE_B_LIMITS, now=NOW
        )
        assert (
            SqlAlchemyJobPersistence(engine).append_event(
                run_id=run_id, job_id=job_id, event_type="run.failed", created_at=NOW
            )
            == 3
        )
        assert (
            SqlAlchemyJobPersistence(engine).append_event(
                run_id=run_id, job_id=job_id, event_type="run.failed", created_at=NOW
            )
            == 4
        )
    finally:
        engine.dispose()
