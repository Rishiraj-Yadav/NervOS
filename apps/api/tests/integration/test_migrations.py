"""Alembic lifecycle and migrated-schema integration tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from nervos_core.infrastructure.database import create_sqlite_engine
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[4]
ALEMBIC_INI = ROOT / "apps" / "api" / "alembic.ini"
APPLICATION_TABLES = {"users", "auth_sessions"}


def alembic_config(database_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """Build Alembic configuration targeting only a disposable test database."""
    monkeypatch.setenv("NERVOS_ENVIRONMENT", "test")
    monkeypatch.setenv("NERVOS_DATABASE_PATH", str(database_path))
    return Config(str(ALEMBIC_INI))


def application_tables(engine: Engine) -> set[str]:
    """Return tables owned by NervOS rather than SQLite or Alembic."""
    return set(inspect(engine).get_table_names()) & APPLICATION_TABLES


def migrate_database(
    database_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Config, Engine]:
    """Upgrade a temporary database and return its configuration and engine."""
    config = alembic_config(database_path, monkeypatch)
    command.upgrade(config, "head")
    return config, create_sqlite_engine(database_path)


def test_upgrade_drift_downgrade_and_reupgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "missing" / "nested" / "migration lifecycle.db"
    config = alembic_config(database_path, monkeypatch)
    assert not database_path.exists()

    command.upgrade(config, "head")
    command.upgrade(config, "head")
    assert database_path.is_file()
    engine = create_sqlite_engine(database_path)
    try:
        assert application_tables(engine) == APPLICATION_TABLES
        with engine.connect() as connection:
            current_revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert current_revision == "0001_stage_a"
            assert connection.scalar(text("PRAGMA foreign_keys")) == 1
            assert connection.scalar(text("PRAGMA busy_timeout")) == 5000
        command.check(config)
    finally:
        engine.dispose()

    command.downgrade(config, "base")
    engine = create_sqlite_engine(database_path)
    try:
        assert application_tables(engine) == set()
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_sqlite_engine(database_path)
    try:
        assert application_tables(engine) == APPLICATION_TABLES
    finally:
        engine.dispose()


def test_migrated_schema_matches_a2_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "schema.db"
    _, engine = migrate_database(database_path, monkeypatch)
    try:
        inspector = inspect(engine)
        assert application_tables(engine) == APPLICATION_TABLES
        assert [column["name"] for column in inspector.get_columns("users")] == [
            "id",
            "username",
            "password_hash",
            "role",
            "is_active",
            "created_at",
            "updated_at",
        ]
        assert [column["name"] for column in inspector.get_columns("auth_sessions")] == [
            "id",
            "user_id",
            "token_hash",
            "created_at",
            "expires_at",
            "revoked_at",
        ]
        assert inspector.get_pk_constraint("users")["constrained_columns"] == ["id"]
        assert inspector.get_pk_constraint("auth_sessions")["constrained_columns"] == ["id"]
        assert {item["name"] for item in inspector.get_unique_constraints("users")} == {
            "uq_users_username"
        }
        assert {item["name"] for item in inspector.get_unique_constraints("auth_sessions")} == {
            "uq_auth_sessions_token_hash"
        }
        assert {item["name"] for item in inspector.get_check_constraints("users")} == {
            "ck_users_username_canonical",
            "ck_users_username_length",
            "ck_users_role_nonempty",
            "ck_users_timestamp_order",
        }
        assert {item["name"] for item in inspector.get_check_constraints("auth_sessions")} == {
            "ck_auth_sessions_expiration_order",
            "ck_auth_sessions_revocation_order",
        }
        foreign_keys = inspector.get_foreign_keys("auth_sessions")
        assert len(foreign_keys) == 1
        assert foreign_keys[0]["name"] == "fk_auth_sessions_user_id_users"
        assert foreign_keys[0]["referred_table"] == "users"
        assert {item["name"] for item in inspector.get_indexes("auth_sessions")} == {
            "ix_auth_sessions_user_id",
            "ix_auth_sessions_expires_at",
        }
        assert inspector.get_indexes("users") == []
        with engine.connect() as connection:
            users_sql = connection.scalar(
                text("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'")
            )
            sessions_sql = connection.scalar(
                text("SELECT sql FROM sqlite_master WHERE type='table' AND name='auth_sessions'")
            )
            assert "AUTOINCREMENT" in str(users_sql)
            assert "PRIMARY KEY AUTOINCREMENT" in str(users_sql)
            assert "AUTOINCREMENT" in str(sessions_sql)
            assert "PRIMARY KEY AUTOINCREMENT" in str(sessions_sql)
            assert connection.scalar(text("SELECT count(*) FROM users")) == 0

            connection.execute(
                text(
                    "INSERT INTO users "
                    "(username, password_hash, role, created_at, updated_at) "
                    "VALUES ('abc', 'fake', 'admin', "
                    "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO users "
                    "(username, password_hash, role, created_at, updated_at) "
                    "VALUES (:username, 'fake', 'admin', "
                    "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                ),
                {"username": "a" * 32},
            )
            assert (
                connection.scalar(text("SELECT is_active FROM users WHERE username = 'abc'")) == 1
            )
    finally:
        engine.dispose()


def test_migrated_constraints_are_enforced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, engine = migrate_database(tmp_path / "constraints.db", monkeypatch)
    valid_user = {
        "username": "admin",
        "password_hash": "fake-password-hash",
        "role": "admin",
        "created_at": "2026-01-01 00:00:00",
        "updated_at": "2026-01-01 00:00:00",
    }
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users "
                    "(username, password_hash, role, created_at, updated_at) "
                    "VALUES (:username, :password_hash, :role, :created_at, :updated_at)"
                ),
                valid_user,
            )

        invalid_users = [
            {**valid_user, "username": "admin"},
            {**valid_user, "username": "Admin"},
            {**valid_user, "username": "ab"},
            {**valid_user, "username": "a" * 33},
            {**valid_user, "username": "second", "role": "   "},
            {
                **valid_user,
                "username": "second",
                "created_at": "2026-01-02 00:00:00",
                "updated_at": "2026-01-01 00:00:00",
            },
        ]
        for values in invalid_users:
            assert_integrity_error(engine, "users", values)

        valid_session = {
            "user_id": 1,
            "token_hash": bytes(range(32)),
            "created_at": "2026-01-01 00:00:00",
            "expires_at": "2026-01-08 00:00:00",
            "revoked_at": None,
        }
        insert_row(engine, "auth_sessions", valid_session)
        invalid_sessions = [
            {**valid_session, "user_id": 999, "token_hash": b"f" * 32},
            {**valid_session},
            {
                **valid_session,
                "token_hash": b"e" * 32,
                "expires_at": "2026-01-01 00:00:00",
            },
            {
                **valid_session,
                "token_hash": b"r" * 32,
                "revoked_at": "2025-12-31 00:00:00",
            },
        ]
        for values in invalid_sessions:
            assert_integrity_error(engine, "auth_sessions", values)
    finally:
        engine.dispose()


def insert_row(engine: Engine, table_name: str, values: dict[str, Any]) -> None:
    """Insert one test row using named parameters."""
    columns = ", ".join(values)
    parameters = ", ".join(f":{column}" for column in values)
    with engine.begin() as connection:
        connection.execute(
            text(f"INSERT INTO {table_name} ({columns}) VALUES ({parameters})"),
            values,
        )


def assert_integrity_error(engine: Engine, table_name: str, values: dict[str, Any]) -> None:
    """Assert that SQLite rejects one invalid row in its own transaction."""
    with pytest.raises(IntegrityError):
        insert_row(engine, table_name, values)
