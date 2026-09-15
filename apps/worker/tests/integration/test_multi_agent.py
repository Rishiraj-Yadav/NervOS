"""C2 multi-Agent integration test: six Instances through one shared engine."""

from __future__ import annotations

from pathlib import Path

import pytest
from nervos_core.application.model_completion import (
    MODEL_UNAVAILABLE,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
)
from nervos_core.domain.runs import STAGE_B_LIMITS
from nervos_core.infrastructure.database import create_sqlite_engine
from nervos_core.infrastructure.database.jobs import SqlAlchemyJobPersistence
from sqlalchemy import Engine, text

NOW = "2026-09-14 00:00:00"
PROVIDERS = ("anthropic", "openai")


def prepare_six_instances(path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    from alembic import command
    from alembic.config import Config
    from support import ROOT

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
        for index in range(1, 7):
            provider = PROVIDERS[(index - 1) % len(PROVIDERS)]
            connection.execute(
                text(
                    "INSERT INTO agent_instances"
                    "(owner_user_id,agent_key,agent_definition_version,display_name,enabled,"
                    "model_provider,model_name,created_at,updated_at) "
                    "VALUES(1,'nervos.chat','1',:d,1,:p,:m,:n,:n)"
                ),
                {
                    "d": f"Agent {index}",
                    "p": provider,
                    "m": f"opaque/{provider}-model",
                    "n": NOW,
                },
            )
    return engine


@pytest.mark.anyio
async def test_six_instances_progress_through_one_shared_engine_without_contamination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Six instances run concurrently through a Worker capped at active=3 without leaking state."""
    from support import RecordingCompletion, build_worker

    engine = prepare_six_instances(tmp_path / "multi.db", monkeypatch)
    try:
        from datetime import UTC, datetime

        now_dt = datetime(2026, 9, 14, tzinfo=UTC)
        run_ids: list[int] = []
        for index in range(1, 7):
            run = SqlAlchemyJobPersistence(engine, max_pending=1000).submit(
                owner_user_id=1,
                agent_instance_id=index,
                input_text=f"input from agent {index}",
                limits=STAGE_B_LIMITS,
                now=now_dt,
            )
            run_ids.append(run.id)

        # Instance 3's provider is forced to fail, to prove it leaves other Instances untouched.
        openai = RecordingCompletion(provider_id="openai", reply="OpenAI reply")

        # Let the first Anthropic call fail terminally, the rest succeed. The failure is
        # deliberately nonretryable: this test proves cross-Instance isolation, so the one forced
        # failure must reach a terminal state rather than acquire C4's durable retry.
        class FlakyAnthropic(RecordingCompletion):
            def __init__(self) -> None:
                super().__init__(provider_id="anthropic", reply="Anthropic reply")
                self._failures = 1

            async def complete(self, request: ModelRequest) -> ModelResponse:
                if self._failures > 0:
                    self._failures -= 1
                    raise ModelProviderError(MODEL_UNAVAILABLE)
                return await super().complete(request)

        completions = {"anthropic": FlakyAnthropic(), "openai": openai}
        worker = build_worker(
            engine,
            completions,  # type: ignore[arg-type]
            concurrency=3,
            max_active=3,
            poll_interval=0.01,
        )
        from support import run_until_stopped

        await run_until_stopped(worker, engine)

        # Every Run reached a terminal state with no cross-contamination.
        with engine.connect() as connection:
            statuses = {
                int(row[0]): str(row[1])
                for row in connection.execute(text("SELECT id, status FROM runs")).all()
            }
        assert len(statuses) == 6
        # The first Anthropic run failed terminally as forced; all other 5 succeeded.
        failed_count = sum(1 for status in statuses.values() if status == "failed")
        succeeded_count = sum(1 for status in statuses.values() if status == "succeeded")
        assert failed_count == 1
        assert succeeded_count == 5

        # Every Job has attempt_count == 1 and no attempt was duplicated.
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM job_attempts")) == 6
            assert connection.scalar(text("SELECT max(attempt_count) FROM jobs")) == 1
    finally:
        engine.dispose()
