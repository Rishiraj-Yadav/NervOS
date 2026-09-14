"""Shared fixtures for API tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
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
from nervos_core.application.agent_definitions import create_builtin_definition_registry
from nervos_core.application.agents import AgentService
from nervos_core.application.authentication import AuthenticationService
from nervos_core.application.model_completion import (
    ModelRequest,
    ModelResponse,
    ModelUsage,
    StopOutcome,
)
from nervos_core.application.model_providers import ModelProviderCatalog
from nervos_core.application.run_coordinator import RunCoordinator
from nervos_core.application.trusted_chat import create_builtin_handler_registry
from nervos_core.infrastructure.database.agents import SqlAlchemyAgentPersistence
from nervos_core.infrastructure.database.authentication import SqlAlchemyAuthenticationPersistence
from nervos_core.infrastructure.database.models import UserRecord
from nervos_core.infrastructure.security.passwords import Argon2PasswordHasher
from nervos_core.infrastructure.security.session_tokens import SecureSessionTokens
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).resolve().parents[3]

# The two production provider identifiers this milestone supports.
PROVIDER_ID = "anthropic"
SECOND_PROVIDER_ID = "openai"

ORIGIN = {"Origin": "http://localhost:5173"}
PASSWORD = "correct horse battery staple"


class DeterministicCompletion:
    """Offline, test-only model completion double.

    It is installed only by test fixtures. Production composition never references it, no
    environment flag or setting can register it, and it performs no network I/O.
    """

    def __init__(
        self, *, text: str = "deterministic answer", provider_id: str = PROVIDER_ID
    ) -> None:
        self.requests: list[ModelRequest] = []
        self.calls = 0
        self.text = text
        self.provider_id = provider_id
        self.finish_reason: StopOutcome | None = StopOutcome.STOP
        self.usage: ModelUsage | None = ModelUsage(11, 3, None)
        self.error: Exception | None = None

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return ModelResponse(
            self.text,
            self.provider_id,
            request.model_name,
            self.finish_reason,
            self.usage,
        )


def fast_password_hasher() -> Argon2PasswordHasher:
    """Return a cheap Argon2id hasher so authentication tests stay fast."""
    return Argon2PasswordHasher(
        PasswordHasher(
            time_cost=1,
            memory_cost=8192,
            parallelism=1,
            hash_len=32,
            type=Type.ID,
        )
    )


def install_catalog(app: FastAPI, catalog: ModelProviderCatalog) -> None:
    """Install a provider catalog and rebuild the Agent service and coordinator over it."""
    handlers = create_builtin_handler_registry()
    service = AgentService(
        SqlAlchemyAgentPersistence(app.state.session_factory),
        create_builtin_definition_registry(),
        utc_now,
        handlers,
        catalog,
    )
    app.state.model_provider_catalog = catalog
    app.state.agent_service = service
    app.state.run_coordinator = RunCoordinator(service, handlers, catalog)


def deterministic_catalog(completion: DeterministicCompletion) -> ModelProviderCatalog:
    """Return a catalog whose only known and configured provider is the deterministic double."""
    return ModelProviderCatalog(
        [(PROVIDER_ID, lambda: completion)],
        known=[PROVIDER_ID],
    )


def unavailable_catalog() -> ModelProviderCatalog:
    """Return a catalog where the known provider has no process configuration."""
    return ModelProviderCatalog([], known=[PROVIDER_ID])


def two_provider_catalog(
    anthropic: DeterministicCompletion, openai: DeterministicCompletion
) -> ModelProviderCatalog:
    """Return a catalog configuring both production providers with distinct doubles.

    Keeping the doubles separate is what makes a provider-routing defect observable: a
    fallback or a misrouted lookup shows up as a call on the wrong recorder.
    """
    return ModelProviderCatalog(
        [(PROVIDER_ID, lambda: anthropic), (SECOND_PROVIDER_ID, lambda: openai)],
        known=[PROVIDER_ID, SECOND_PROVIDER_ID],
    )


def unavailable_two_provider_catalog() -> ModelProviderCatalog:
    """Return a catalog where both known providers lack process configuration."""
    return ModelProviderCatalog([], known=[PROVIDER_ID, SECOND_PROVIDER_ID])


def seed_user(session_factory: sessionmaker[Session], username: str) -> int:
    """Insert one active user with a real password hash and return its identifier."""
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        record = UserRecord(
            username=username,
            password_hash=fast_password_hasher().hash(PASSWORD),
            role="admin",
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        session.add(record)
        session.flush()
        return record.id


def create_first_admin(client: TestClient, username: str = "owner") -> None:
    """Perform first-run setup, leaving the client authenticated."""
    response = client.post(
        "/api/v1/setup",
        json={"username": username, "password": PASSWORD},
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text


def sign_in(client: TestClient, username: str) -> None:
    """Log an existing user in on this client."""
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PASSWORD},
        headers=ORIGIN,
    )
    assert response.status_code == 200, response.text


@pytest.fixture(autouse=True)
def clear_settings_cache() -> Iterator[None]:
    """Prevent process-setting cache state from leaking between tests."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def deterministic_completion() -> DeterministicCompletion:
    """Expose the test-only provider double installed into the composed application."""
    return DeterministicCompletion()


@pytest.fixture
def migrated_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deterministic_completion: DeterministicCompletion,
) -> Iterator[tuple[FastAPI, Settings]]:
    """Compose an API over one migrated disposable file database.

    The composed provider catalog is replaced with an offline deterministic one so no API test
    can reach a real provider, even when the developer's environment holds a credential.
    """
    database_path = tmp_path / "api-authentication.db"
    settings = Settings(environment="test", database_path=database_path)
    monkeypatch.setenv("NERVOS_ENVIRONMENT", "test")
    monkeypatch.setenv("NERVOS_DATABASE_PATH", str(database_path))
    command.upgrade(Config(str(ROOT / "apps" / "api" / "alembic.ini")), "head")
    app = create_app(settings)
    app.state.authentication_service = AuthenticationService(
        SqlAlchemyAuthenticationPersistence(app.state.session_factory),
        fast_password_hasher(),
        SecureSessionTokens(),
        utc_now,
    )
    install_catalog(app, deterministic_catalog(deterministic_completion))
    yield app, settings


@pytest.fixture
def client(migrated_app: tuple[FastAPI, Settings]) -> Iterator[TestClient]:
    """Run an API client with deterministic lifespan cleanup."""
    app, _ = migrated_app
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def app_under_test(migrated_app: tuple[FastAPI, Settings]) -> FastAPI:
    """Expose the composed application for assertions about composed collaborators."""
    app, _ = migrated_app
    return app


@pytest.fixture
def owner_client(client: TestClient) -> TestClient:
    """Return the shared client authenticated as the first administrator."""
    create_first_admin(client)
    return client


@pytest.fixture
def seed_user_account(migrated_app: tuple[FastAPI, Settings]) -> Callable[[str], int]:
    """Return a factory that inserts an unrelated active user and yields its identifier."""
    app, _ = migrated_app

    def seed(username: str) -> int:
        return seed_user(app.state.session_factory, username)

    return seed


@pytest.fixture
def sign_in_as(client: TestClient) -> Callable[[str], None]:
    """Return a factory that replaces this client's session with another user's."""

    def sign_in_switching(username: str) -> None:
        # Revoke the current session first so the client ends up holding exactly one identity.
        client.post("/api/v1/auth/logout", headers=ORIGIN)
        sign_in(client, username)

    return sign_in_switching


@pytest.fixture
def install_provider_catalog(
    migrated_app: tuple[FastAPI, Settings],
) -> Callable[[ModelProviderCatalog], None]:
    """Return a factory that swaps the composed provider catalog for another one."""
    app, _ = migrated_app

    def install(catalog: ModelProviderCatalog) -> None:
        install_catalog(app, catalog)

    return install


@pytest.fixture
def unconfigured_provider_catalog() -> ModelProviderCatalog:
    """Return a catalog where the known provider has no process configuration."""
    return unavailable_catalog()
