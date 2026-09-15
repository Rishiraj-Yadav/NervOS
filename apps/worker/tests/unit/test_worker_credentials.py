"""Worker credential containment and sentinel-leak regression tests."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from nervos_core.application.model_completion import ModelRequest, ModelResponse
from nervos_worker.app import create_worker
from nervos_worker.config import WorkerSettings
from sqlalchemy import text

SENTINEL_CREDENTIAL = "SYNTHETIC-LEAK-DO-NOT-PERSIST"


def test_the_worker_reads_credentials_only_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The key builds its client once; nothing reachable afterwards carries the value."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", SENTINEL_CREDENTIAL)
    monkeypatch.setenv("OPENAI_API_KEY", SENTINEL_CREDENTIAL)
    settings = WorkerSettings(database_path=tmp_path / "creds.db")

    with caplog.at_level(logging.DEBUG):
        composition = create_worker(settings)
    try:
        assert SENTINEL_CREDENTIAL not in repr(composition.settings)
        assert SENTINEL_CREDENTIAL not in str(composition.settings.model_dump())
        assert SENTINEL_CREDENTIAL not in repr(composition.providers.catalog)
        assert SENTINEL_CREDENTIAL not in caplog.text
    finally:
        composition.engine.dispose()


@pytest.mark.anyio
async def test_a_raw_exception_never_leaks_into_database_or_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An injected raw exception text must not appear in any row, log, or response."""
    from support import PROVIDER_ID, build_worker, migrate, run_row, submit

    engine = migrate(tmp_path / "leak.db", monkeypatch)
    try:
        run_id = submit(engine)

        class LeakingCompletion:
            def __init__(self) -> None:
                self.calls = 0

            async def complete(self, request: ModelRequest) -> ModelResponse:
                del request
                self.calls += 1
                raise RuntimeError(f"boom: {SENTINEL_CREDENTIAL}")

        worker = build_worker(engine, {PROVIDER_ID: LeakingCompletion()})
        from support import run_until_stopped

        with caplog.at_level(logging.DEBUG):
            await run_until_stopped(worker, engine)

        # The error is normalized to internal_execution_error with its static message.
        row = run_row(engine, run_id)
        assert row["status"] == "failed"
        assert row["error_code"] == "internal_execution_error"
        assert SENTINEL_CREDENTIAL not in str(row["error_message"] or "")
        assert SENTINEL_CREDENTIAL not in caplog.text

        with engine.connect() as connection:
            for table in ("runs", "jobs", "job_attempts", "run_events"):
                dumped = " ".join(
                    str(tuple(r)) for r in connection.execute(text(f"SELECT * FROM {table}")).all()
                )
                assert SENTINEL_CREDENTIAL not in dumped, table
    finally:
        engine.dispose()
