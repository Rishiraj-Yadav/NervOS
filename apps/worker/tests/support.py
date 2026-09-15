"""Shared fixtures for the C2 Worker tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from nervos_core.application.job_execution import JobExecutionService
from nervos_core.application.model_completion import (
    ModelCompletion,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    StopOutcome,
)
from nervos_core.application.run_execution import RunExecutor
from nervos_core.application.trusted_chat import create_builtin_handler_registry
from nervos_core.domain.runs import STAGE_B_LIMITS
from nervos_core.infrastructure.database import create_sqlite_engine
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobExecutionPersistence,
    SqlAlchemyJobPersistence,
)
from nervos_worker.service import Worker
from sqlalchemy import Engine, text

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 9, 14, tzinfo=UTC)
PROVIDER_ID = "anthropic"
SECOND_PROVIDER_ID = "openai"


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
                "VALUES(1,'nervos.chat','1','Chat',1,:p,'opaque/model',:n,:n)"
            ),
            {"n": NOW, "p": PROVIDER_ID},
        )
    return engine


def submit(engine: Engine, *, text_value: str = "hello") -> int:
    """Durably accept one Run and return its identifier."""
    run = SqlAlchemyJobPersistence(engine, max_pending=1000).submit(
        owner_user_id=1,
        agent_instance_id=1,
        input_text=text_value,
        limits=STAGE_B_LIMITS,
        now=NOW,
    )
    return run.id


def counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            table: int(connection.scalar(text(f"SELECT count(*) FROM {table}")) or 0)
            for table in ("runs", "jobs", "job_attempts", "run_events")
        }


def run_row(engine: Engine, run_id: int) -> dict[str, object]:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text(
                    "SELECT status, output_text, finish_reason, error_code, error_message,"
                    " elapsed_ms FROM runs WHERE id=:r"
                ),
                {"r": run_id},
            )
            .mappings()
            .one()
        )


def job_row(engine: Engine, run_id: int) -> dict[str, object]:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text(
                    "SELECT id, status, attempt_count, claimed_by, claim_token FROM jobs"
                    " WHERE run_id=:r"
                ),
                {"r": run_id},
            )
            .mappings()
            .one()
        )


def attempt_rows(engine: Engine) -> list[dict[str, object]]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT attempt_number, status, retry_disposition, error_code"
                    " FROM job_attempts ORDER BY id"
                )
            )
            .mappings()
            .all()
        ]


def event_types(engine: Engine, run_id: int) -> list[str]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                text("SELECT event_type FROM run_events WHERE run_id=:r ORDER BY sequence"),
                {"r": run_id},
            ).scalars()
        )


class RecordingCompletion:
    """Deterministic offline completion double recording every provider call."""

    def __init__(self, provider_id: str = PROVIDER_ID, reply: str = "worker answer") -> None:
        self.provider_id = provider_id
        self.reply = reply
        self.calls = 0
        self.requests: list[ModelRequest] = []
        self.error: Exception | None = None

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return ModelResponse(
            self.reply,
            self.provider_id,
            request.model_name,
            StopOutcome.STOP,
            ModelUsage(11, 7, 18),
        )


def build_execution_service(
    engine: Engine, completions: dict[str, ModelCompletion]
) -> tuple[SqlAlchemyJobExecutionPersistence, JobExecutionService]:
    """Compose the shipped execution service over disposable persistence."""
    persistence = SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None)
    service = JobExecutionService(
        persistence,
        RunExecutor(create_builtin_handler_registry()),
        completions,
        lambda: NOW,
    )
    return persistence, service


def build_worker(
    engine: Engine,
    completions: dict[str, ModelCompletion],
    *,
    worker_id: str = "worker-1",
    concurrency: int = 1,
    max_active: int = 4,
    poll_interval: float = 0.01,
    idle_max: float = 0.02,
    shutdown_grace: float = 2.0,
) -> Worker:
    """Compose the shipped Worker loop over disposable persistence."""
    _persistence, execution = build_execution_service(engine, completions)
    return Worker(
        _persistence,
        execution,
        completions,
        clock=lambda: NOW,
        worker_id=worker_id,
        concurrency=concurrency,
        max_active=max_active,
        poll_interval=poll_interval,
        idle_max=idle_max,
        shutdown_grace=shutdown_grace,
    )


async def run_until_stopped(worker: Worker, engine: Engine) -> None:
    """Run the Worker until every submitted Run reaches a terminal state, then stop."""
    import asyncio

    stop = asyncio.Event()

    async def watch() -> None:
        for _ in range(2000):
            await asyncio.sleep(0.02)
            with engine.connect() as connection:
                active = int(
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM jobs"
                            " WHERE status IN ('queued','claimed','running')"
                        )
                    )
                    or 0
                )
            if active == 0:
                break
        stop.set()

    await asyncio.gather(worker.run(stop), watch())
