"""Shared fixtures for API tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from argon2 import PasswordHasher
from argon2.low_level import Type
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nervos_api.api.dependencies import utc_now
from nervos_api.app import create_app
from nervos_api.config import Settings, get_settings
from nervos_core.application.authentication import AuthenticationService
from nervos_core.infrastructure.database.authentication import SqlAlchemyAuthenticationPersistence
from nervos_core.infrastructure.security.passwords import Argon2PasswordHasher
from nervos_core.infrastructure.security.session_tokens import SecureSessionTokens

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def clear_settings_cache() -> Iterator[None]:
    """Prevent process-setting cache state from leaking between tests."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def migrated_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[FastAPI, Settings]]:
    """Compose an API over one migrated disposable file database."""
    database_path = tmp_path / "api-authentication.db"
    settings = Settings(environment="test", database_path=database_path)
    monkeypatch.setenv("NERVOS_ENVIRONMENT", "test")
    monkeypatch.setenv("NERVOS_DATABASE_PATH", str(database_path))
    command.upgrade(Config(str(ROOT / "apps" / "api" / "alembic.ini")), "head")
    app = create_app(settings)
    app.state.authentication_service = AuthenticationService(
        SqlAlchemyAuthenticationPersistence(app.state.session_factory),
        Argon2PasswordHasher(
            PasswordHasher(
                time_cost=1,
                memory_cost=8192,
                parallelism=1,
                hash_len=32,
                type=Type.ID,
            )
        ),
        SecureSessionTokens(),
        utc_now,
    )
    yield app, settings


@pytest.fixture
def client(migrated_app: tuple[FastAPI, Settings]) -> Iterator[TestClient]:
    """Run an API client with deterministic lifespan cleanup."""
    app, _ = migrated_app
    with TestClient(app) as test_client:
        yield test_client
