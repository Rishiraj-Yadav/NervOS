"""File-backed SQLite concurrency test for one-time setup."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest
from alembic import command
from alembic.config import Config
from nervos_core.application.authentication import SetupComplete
from nervos_core.infrastructure.database import create_session_factory, create_sqlite_engine
from nervos_core.infrastructure.database.authentication import SqlAlchemyAuthenticationPersistence
from nervos_core.infrastructure.database.models import AuthSessionRecord, UserRecord
from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[4]
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_two_concurrent_setup_attempts_create_exactly_one_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "setup-race.db"
    monkeypatch.setenv("NERVOS_ENVIRONMENT", "test")
    monkeypatch.setenv("NERVOS_DATABASE_PATH", str(database_path))
    command.upgrade(Config(str(ROOT / "apps" / "api" / "alembic.ini")), "head")
    engine = create_sqlite_engine(database_path)
    factory = create_session_factory(engine)
    persistence = SqlAlchemyAuthenticationPersistence(factory)
    barrier = Barrier(2)

    def attempt(number: int) -> str:
        barrier.wait()
        try:
            persistence.create_initial_administrator(
                username=f"admin-{number}",
                password_hash=f"$argon2id$fake-{number}",
                token_hash=bytes([number]) * 32,
                created_at=NOW,
                expires_at=datetime(2026, 1, 8, tzinfo=UTC),
            )
        except SetupComplete:
            return "complete"
        return "created"

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = sorted(executor.map(attempt, (1, 2)))
        assert results == ["complete", "created"]
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(UserRecord)) == 1
            assert session.scalar(select(func.count()).select_from(AuthSessionRecord)) == 1
            auth_session = session.scalar(select(AuthSessionRecord))
            user = session.scalar(select(UserRecord))
            assert auth_session is not None
            assert user is not None
            assert auth_session.user_id == user.id
    finally:
        engine.dispose()
