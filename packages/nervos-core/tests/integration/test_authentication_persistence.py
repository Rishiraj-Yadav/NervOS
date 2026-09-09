"""Integration tests for authentication persistence and service behavior."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from argon2 import PasswordHasher
from argon2.low_level import Type
from nervos_core.application.authentication import (
    AuthenticationRequired,
    AuthenticationService,
    InvalidCredentials,
    SetupComplete,
)
from nervos_core.infrastructure.database import create_session_factory, create_sqlite_engine
from nervos_core.infrastructure.database.authentication import SqlAlchemyAuthenticationPersistence
from nervos_core.infrastructure.database.models import AuthSessionRecord, UserRecord
from nervos_core.infrastructure.security.passwords import Argon2PasswordHasher
from nervos_core.infrastructure.security.session_tokens import SecureSessionTokens
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).resolve().parents[4]
NOW = datetime(2026, 1, 1, tzinfo=UTC)
PASSWORD = "correct horse battery staple"


@pytest.fixture
def authentication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[AuthenticationService, sessionmaker[Session]]]:
    database_path = tmp_path / "authentication.db"
    monkeypatch.setenv("NERVOS_ENVIRONMENT", "test")
    monkeypatch.setenv("NERVOS_DATABASE_PATH", str(database_path))
    command.upgrade(Config(str(ROOT / "apps" / "api" / "alembic.ini")), "head")
    engine = create_sqlite_engine(database_path)
    factory = create_session_factory(engine)
    password_hasher = Argon2PasswordHasher(
        PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1, hash_len=32, type=Type.ID)
    )
    service = AuthenticationService(
        SqlAlchemyAuthenticationPersistence(factory),
        password_hasher,
        SecureSessionTokens(),
        lambda: NOW,
    )
    yield service, factory
    engine.dispose()


def test_setup_stores_one_admin_hash_and_digest(
    authentication: tuple[AuthenticationService, sessionmaker[Session]],
) -> None:
    service, factory = authentication

    issued = service.setup("  Admin.User ", PASSWORD)

    assert issued.user.username == "admin.user"
    assert issued.user.role == "admin"
    assert issued.user.is_active is True
    assert issued.expires_at == NOW + timedelta(days=7)
    with factory() as session:
        user = session.scalar(select(UserRecord))
        stored_session = session.scalar(select(AuthSessionRecord))
        assert user is not None
        assert user.password_hash.startswith("$argon2id$")
        assert user.password_hash != PASSWORD
        assert stored_session is not None
        assert stored_session.token_hash == SecureSessionTokens().digest(issued.token)
        assert len(stored_session.token_hash) == 32
        assert issued.token.encode() not in stored_session.token_hash


def test_setup_uses_generated_identifier_without_fixed_id_invariant(
    authentication: tuple[AuthenticationService, sessionmaker[Session]],
) -> None:
    service, factory = authentication
    with factory.begin() as session:
        session.execute(
            text(
                "INSERT INTO users "
                "(username, password_hash, role, is_active, created_at, updated_at) "
                "VALUES ('temporary', 'not-used', 'admin', 1, :now, :now)"
            ),
            {"now": NOW.replace(tzinfo=None)},
        )
        session.execute(text("DELETE FROM users WHERE username = 'temporary'"))

    issued = service.setup("real-admin", PASSWORD)

    assert issued.user.id > 1
    with factory() as session:
        stored = session.scalar(select(AuthSessionRecord))
        assert stored is not None
        assert stored.user_id == issued.user.id


def test_repeated_setup_never_creates_second_user(
    authentication: tuple[AuthenticationService, sessionmaker[Session]],
) -> None:
    service, factory = authentication
    service.setup("first-user", PASSWORD)

    with pytest.raises(SetupComplete):
        service.setup("different-user", PASSWORD)

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(UserRecord)) == 1
        assert session.scalar(select(func.count()).select_from(AuthSessionRecord)) == 1


def test_successful_login_rehashes_obsolete_password(
    authentication: tuple[AuthenticationService, sessionmaker[Session]],
) -> None:
    service, factory = authentication
    service.setup("admin", PASSWORD)
    obsolete = PasswordHasher(
        time_cost=1,
        memory_cost=4096,
        parallelism=1,
        hash_len=16,
        type=Type.ID,
    ).hash(PASSWORD)
    with factory.begin() as session:
        user = session.scalar(select(UserRecord))
        assert user is not None
        user.password_hash = obsolete

    service.login("admin", PASSWORD)

    with factory() as session:
        user = session.scalar(select(UserRecord))
        assert user is not None
        assert user.password_hash != obsolete
        assert user.password_hash.startswith("$argon2id$")


def test_login_authenticate_expire_revoke_and_inactive(
    authentication: tuple[AuthenticationService, sessionmaker[Session]],
) -> None:
    service, factory = authentication
    setup_session = service.setup("admin", PASSWORD)

    with pytest.raises(InvalidCredentials):
        service.login("unknown", PASSWORD)
    with pytest.raises(InvalidCredentials):
        service.login("admin", "incorrect password")

    login_session = service.login("ADMIN", PASSWORD)
    assert login_session.token != setup_session.token
    assert service.authenticate(login_session.token) == login_session.user

    service.logout(login_session.token)
    with pytest.raises(AuthenticationRequired):
        service.authenticate(login_session.token)

    with factory.begin() as session:
        stored = session.scalar(
            select(AuthSessionRecord).where(
                AuthSessionRecord.token_hash == SecureSessionTokens().digest(setup_session.token)
            )
        )
        assert stored is not None
        stored.created_at = NOW - timedelta(days=1)
        stored.expires_at = NOW
    with pytest.raises(AuthenticationRequired):
        service.authenticate(setup_session.token)

    with factory.begin() as session:
        user = session.scalar(select(UserRecord))
        assert user is not None
        user.is_active = False
    with pytest.raises(InvalidCredentials):
        service.login("admin", PASSWORD)
