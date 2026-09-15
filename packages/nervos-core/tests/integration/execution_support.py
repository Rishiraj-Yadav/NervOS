"""Shared fixtures for the C2 durable-execution persistence tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from nervos_core.infrastructure.database import create_sqlite_engine
from sqlalchemy import Engine, text

ROOT = Path(__file__).resolve().parents[4]
NOW = datetime(2026, 9, 14, tzinfo=UTC)


def migrate(path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    """Create one disposable migrated database holding a user and a Chat Agent Instance."""
    monkeypatch.setenv("NERVOS_DATABASE_PATH", str(path))
    command.upgrade(Config(str(ROOT / "apps" / "api" / "alembic.ini")), "head")
    engine = create_sqlite_engine(path)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users"
                "(username,password_hash,role,is_active,created_at,updated_at) "
                "VALUES('owner',X'00','admin',1,:n,:n)"
            ),
            {"n": NOW},
        )
        connection.execute(
            text(
                "INSERT INTO agent_instances"
                "(owner_user_id,agent_key,agent_definition_version,display_name,enabled,"
                "model_provider,model_name,created_at,updated_at) "
                "VALUES(1,'nervos.chat','1','Chat',1,'anthropic','opaque/model',:n,:n)"
            ),
            {"n": NOW},
        )
    return engine


def counts(engine: Engine) -> dict[str, int]:
    """Return the row counts of every durable execution table."""
    with engine.connect() as connection:
        return {
            table: int(connection.scalar(text(f"SELECT count(*) FROM {table}")) or 0)
            for table in ("runs", "jobs", "job_attempts", "run_events")
        }


def job_id_of(engine: Engine, run_id: int) -> int:
    """Return the unique Job identifier of one Run."""
    with engine.connect() as connection:
        return int(connection.scalar(text("SELECT id FROM jobs WHERE run_id=:r"), {"r": run_id}))


def job_row(engine: Engine, job_id: int) -> dict[str, object]:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text(
                    "SELECT status, attempt_count, max_attempts, claimed_by, claim_token,"
                    " lease_expires_at, last_heartbeat_at, error_code, error_message,"
                    " available_at, finished_at"
                    " FROM jobs WHERE id=:j"
                ),
                {"j": job_id},
            )
            .mappings()
            .one()
        )


def attempt_row(engine: Engine, attempt_id: int) -> dict[str, object]:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text(
                    "SELECT status, execution_started_at, finished_at, retry_disposition,"
                    " error_code, worker_id, claim_token FROM job_attempts WHERE id=:a"
                ),
                {"a": attempt_id},
            )
            .mappings()
            .one()
        )


def run_row(engine: Engine, run_id: int) -> dict[str, object]:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text(
                    "SELECT status, started_at, finished_at, output_text, finish_reason,"
                    " error_code, error_message, elapsed_ms FROM runs WHERE id=:r"
                ),
                {"r": run_id},
            )
            .mappings()
            .one()
        )


def event_types(engine: Engine, run_id: int) -> list[str]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                text("SELECT event_type FROM run_events WHERE run_id=:r ORDER BY sequence"),
                {"r": run_id},
            ).scalars()
        )
