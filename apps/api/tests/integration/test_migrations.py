"""Alembic lifecycle and migrated-schema integration tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from nervos_api.config import Settings
from nervos_core.infrastructure.database import (
    Base,
    create_sqlite_engine,
)
from nervos_core.infrastructure.database import (
    models as database_models,
)
from sqlalchemy import CheckConstraint, Engine, String, inspect, text
from sqlalchemy.exc import IntegrityError

# Importing the ORM models registers Base.metadata, the authoritative schema contract that the
# C1 parity test compares the migrated SQLite schema against.
_ = database_models

ROOT = Path(__file__).resolve().parents[4]
ALEMBIC_INI = ROOT / "apps" / "api" / "alembic.ini"
APPLICATION_TABLES = {
    "users",
    "auth_sessions",
    "agent_instances",
    "runs",
    "jobs",
    "job_attempts",
    "run_events",
    "workers",
    "queue_partitions",
}
DEFAULT_DATABASE = (Path.home() / ".nervos" / "nervos.db").resolve(strict=False)


def database_fingerprint(path: Path) -> tuple[bool, int, int]:
    """Identify a database file without reading any of its contents."""
    if not path.exists():
        return (False, 0, 0)
    stat = path.stat()
    return (True, stat.st_size, stat.st_mtime_ns)


@pytest.fixture(autouse=True)
def default_database_is_untouched() -> Iterator[None]:
    """Prove migration tests neither create nor mutate the default database."""
    before = database_fingerprint(DEFAULT_DATABASE)
    yield
    assert database_fingerprint(DEFAULT_DATABASE) == before


def alembic_config(database_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """Build Alembic configuration targeting only a disposable test database."""
    monkeypatch.setenv("NERVOS_ENVIRONMENT", "test")
    monkeypatch.setenv("NERVOS_DATABASE_PATH", str(database_path))
    resolved = database_path.resolve(strict=False)
    assert resolved != DEFAULT_DATABASE
    assert Settings().database_path == resolved
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
            assert current_revision == "0006_stage_c6_queue_partitions"
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


def test_populated_stage_a_survives_b1_downgrade_and_reupgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "populated-lifecycle.db"
    config = alembic_config(path, monkeypatch)
    command.upgrade(config, "0001_stage_a")
    engine = create_sqlite_engine(path)
    try:
        insert_row(
            engine,
            "users",
            {
                "username": "admin",
                "password_hash": "fake",
                "role": "admin",
                "created_at": "2026-01-01 00:00:00",
                "updated_at": "2026-01-01 00:00:00",
            },
        )
        insert_row(
            engine,
            "auth_sessions",
            {
                "user_id": 1,
                "token_hash": bytes(range(32)),
                "created_at": "2026-01-01 00:00:00",
                "expires_at": "2026-01-08 00:00:00",
                "revoked_at": None,
            },
        )
    finally:
        engine.dispose()
    command.upgrade(config, "head")
    engine = create_sqlite_engine(path)
    try:
        assert application_tables(engine) == APPLICATION_TABLES
        assert engine.connect().scalar(text("SELECT count(*) FROM agent_instances")) == 0
        assert engine.connect().scalar(text("SELECT count(*) FROM runs")) == 0
    finally:
        engine.dispose()
    command.downgrade(config, "0001_stage_a")
    engine = create_sqlite_engine(path)
    try:
        assert application_tables(engine) == {"users", "auth_sessions"}
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT username FROM users")) == "admin"
            assert connection.scalar(text("SELECT count(*) FROM auth_sessions")) == 1
    finally:
        engine.dispose()
    command.upgrade(config, "head")
    command.check(config)
    engine = create_sqlite_engine(path)
    try:
        assert application_tables(engine) == APPLICATION_TABLES
        assert {item["name"] for item in inspect(engine).get_indexes("runs")} == {
            "ix_runs_agent_instance_id_id"
        }
    finally:
        engine.dispose()


def test_b1_migrated_lifecycle_shapes_are_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, engine = migrate_database(tmp_path / "b1-shapes.db", monkeypatch)
    try:
        insert_row(
            engine,
            "users",
            {
                "username": "admin",
                "password_hash": "fake",
                "role": "admin",
                "created_at": "2026-01-01 00:00:00",
                "updated_at": "2026-01-01 00:00:00",
            },
        )
        instance = {
            "owner_user_id": 1,
            "agent_key": "nervos.chat",
            "agent_definition_version": "1",
            "display_name": "Chat",
            "enabled": True,
            "model_provider": "test-provider",
            "model_name": "model",
            "created_at": "2026-01-01 00:00:00",
            "updated_at": "2026-01-01 00:00:00",
        }
        insert_row(engine, "agent_instances", instance)
        base = {
            "agent_instance_id": 1,
            "status": "created",
            "agent_key": "nervos.chat",
            "agent_definition_version": "1",
            "model_provider": "test-provider",
            "model_name": "model",
            "input_text": "hello",
            "input_max_bytes": 8000,
            "input_max_code_points": 4000,
            "output_max_bytes": 32000,
            "output_max_code_points": 16000,
            "provider_timeout_ms": 60000,
            "max_output_tokens": 1024,
            "max_model_calls": 1,
            "output_text": None,
            "finish_reason": None,
            "error_code": None,
            "error_message": None,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "elapsed_ms": None,
            "created_at": "2026-01-01 00:00:00",
            "started_at": None,
            "finished_at": None,
        }
        valid = [
            base,
            {**base, "status": "running", "started_at": "2026-01-01 00:00:01"},
            {
                **base,
                "status": "succeeded",
                "started_at": "2026-01-01 00:00:01",
                "finished_at": "2026-01-01 00:00:02",
                "output_text": "ok",
                "elapsed_ms": 1,
            },
            {
                **base,
                "status": "failed",
                "started_at": "2026-01-01 00:00:01",
                "finished_at": "2026-01-01 00:00:02",
                "error_code": "model_error",
                "error_message": "safe",
                "elapsed_ms": 1,
                "input_tokens": 2,
            },
        ]
        for row in valid:
            insert_row(engine, "runs", row)
        invalid = [
            {**base, "output_text": "\t"},
            {**base, "output_text": "\n"},
            {**base, "output_text": "\r\n"},
            {**base, "output_text": " \t\r\n "},
            {**base, "output_text": chr(0x00A0) + chr(0x2003)},
            {
                **base,
                "status": "failed",
                "started_at": "2026-01-01 00:00:01",
                "finished_at": "2026-01-01 00:00:02",
                "error_code": "model_error",
                "error_message": chr(9),
                "elapsed_ms": 1,
            },
            {**base, "elapsed_ms": 0},
            {**base, "status": "running"},
            {**base, "status": "running", "started_at": "2026-01-01 00:00:01", "input_tokens": 0},
            {
                **base,
                "status": "succeeded",
                "started_at": "2026-01-01 00:00:01",
                "finished_at": "2026-01-01 00:00:02",
                "output_text": " ",
                "elapsed_ms": 1,
            },
            {
                **base,
                "status": "failed",
                "started_at": "2026-01-01 00:00:01",
                "finished_at": "2026-01-01 00:00:02",
                "error_code": "model_error",
                "error_message": " ",
                "elapsed_ms": 1,
            },
            {
                **base,
                "status": "failed",
                "started_at": "2026-01-01 00:00:01",
                "finished_at": "2026-01-01 00:00:02",
                "error_code": "model_error",
                "error_message": "safe",
                "output_text": "bad",
                "elapsed_ms": 1,
            },
            {**base, "status": "unknown"},
            {**base, "input_tokens": -1},
            {**base, "input_max_bytes": 0},
        ]
        for row in invalid:
            assert_integrity_error(engine, "runs", row)
        insert_row(
            engine,
            "runs",
            {
                **base,
                "status": "succeeded",
                "started_at": "2026-01-01 00:00:01",
                "finished_at": "2026-01-01 00:00:02",
                "output_text": chr(9) + "answer" + chr(10),
                "elapsed_ms": 1,
            },
        )
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


C1_TABLES = ("jobs", "job_attempts", "run_events")


def normalize_sql(expression: str) -> str:
    """Collapse whitespace so formatting alone can never mask a semantic difference."""
    return " ".join(str(expression).split()).lower()


def snapshot_run() -> dict[str, Any]:
    """Return a valid immutable-snapshot Run row in the `created` lifecycle shape."""
    return {
        "agent_instance_id": 1,
        "status": "created",
        "agent_key": "nervos.chat",
        "agent_definition_version": "1",
        "model_provider": "test-provider",
        "model_name": "model",
        "input_text": "hello",
        "input_max_bytes": 8000,
        "input_max_code_points": 4000,
        "output_max_bytes": 32000,
        "output_max_code_points": 16000,
        "provider_timeout_ms": 60000,
        "max_output_tokens": 1024,
        "max_model_calls": 1,
        "output_text": None,
        "finish_reason": None,
        "error_code": None,
        "error_message": None,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "elapsed_ms": None,
        "created_at": "2026-01-01 00:00:00",
        "started_at": None,
        "finished_at": None,
    }


def seed_c1_parents(engine: Engine, run_count: int) -> None:
    """Insert one owner, one Agent Instance, and `run_count` immutable-snapshot Runs."""
    insert_row(
        engine,
        "users",
        {
            "username": "admin",
            "password_hash": "fake",
            "role": "admin",
            "created_at": "2026-01-01 00:00:00",
            "updated_at": "2026-01-01 00:00:00",
        },
    )
    insert_row(
        engine,
        "agent_instances",
        {
            "owner_user_id": 1,
            "agent_key": "nervos.chat",
            "agent_definition_version": "1",
            "display_name": "Chat",
            "enabled": True,
            "model_provider": "test-provider",
            "model_name": "model",
            "created_at": "2026-01-01 00:00:00",
            "updated_at": "2026-01-01 00:00:00",
        },
    )
    for _ in range(run_count):
        insert_row(engine, "runs", snapshot_run())


def queued_job() -> dict[str, Any]:
    """Return a valid `queued` Job row, the shape dormant C1 submission persists."""
    return {
        "run_id": 1,
        "agent_instance_id": 1,
        "model_provider": "test-provider",
        "status": "queued",
        "available_at": "2026-01-01 00:00:00",
        "attempt_count": 0,
        "max_attempts": 3,
        "cancel_requested_at": None,
        "claimed_by": None,
        "claim_token": None,
        "lease_expires_at": None,
        "last_heartbeat_at": None,
        "error_code": None,
        "error_message": None,
        "created_at": "2026-01-01 00:00:00",
        "updated_at": "2026-01-01 00:00:00",
        "finished_at": None,
    }


def claimed_attempt() -> dict[str, Any]:
    """Return a valid `claimed` Attempt row, the shape a C2 claim commits."""
    return {
        "job_id": 1,
        "attempt_number": 1,
        "status": "claimed",
        "worker_id": "worker-1",
        "claim_token": b"a" * 32,
        "claimed_at": "2026-01-01 00:00:00",
        "execution_started_at": None,
        "lease_expires_at": "2026-01-01 00:01:00",
        "last_heartbeat_at": "2026-01-01 00:00:00",
        "finished_at": None,
        "retry_disposition": None,
        "error_code": None,
        "error_message": None,
        "created_at": "2026-01-01 00:00:00",
    }


def test_migrated_schema_declares_exactly_the_orm_check_constraints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Permanent parity guard against a silently weaker durable database contract.

    Alembic's `command.check` cannot catch this drift: the installed version does not compare
    SQLite CHECK constraints during autogenerate, so a migration that omits a constraint the
    ORM declares still reports "no changes". The applied schema is compared here instead, on
    both constraint names and normalized expressions, so neither a missing, an extra, nor a
    merely reworded constraint can reappear unnoticed.
    """
    _, engine = migrate_database(tmp_path / "check-parity.db", monkeypatch)
    try:
        inspector = inspect(engine)
        assert set(Base.metadata.tables) == APPLICATION_TABLES
        for table_name, table in Base.metadata.tables.items():
            declared = {
                constraint.name: normalize_sql(constraint.sqltext)
                for constraint in table.constraints
                if isinstance(constraint, CheckConstraint) and constraint.name
            }
            applied = {
                item["name"]: normalize_sql(item["sqltext"])
                for item in inspector.get_check_constraints(table_name)
            }
            assert applied == declared, table_name
    finally:
        engine.dispose()


def test_migrated_c1_columns_defaults_keys_and_indexes_match_the_orm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The applied C1 columns, defaults, keys, and indexes match the reviewed contract."""
    _, engine = migrate_database(tmp_path / "c1-metadata.db", monkeypatch)
    try:
        inspector = inspect(engine)
        jobs = {column["name"]: column for column in inspector.get_columns("jobs")}
        assert jobs["attempt_count"]["default"] == "'0'"
        assert jobs["max_attempts"]["default"] == "'3'"
        for column in (
            "run_id",
            "agent_instance_id",
            "model_provider",
            "status",
            "available_at",
            "attempt_count",
            "max_attempts",
            "created_at",
            "updated_at",
        ):
            assert jobs[column]["nullable"] is False, column
        for column in (
            "cancel_requested_at",
            "claimed_by",
            "claim_token",
            "lease_expires_at",
            "last_heartbeat_at",
            "error_code",
            "error_message",
            "finished_at",
        ):
            assert jobs[column]["nullable"] is True, column
        # SQLite reflects no length for BLOB, so the 32-byte token invariant is asserted
        # through the CHECK constraints above rather than through column metadata.
        claimed_by_type = jobs["claimed_by"]["type"]
        assert isinstance(claimed_by_type, String)
        assert claimed_by_type.length == 128

        attempts = {column["name"]: column for column in inspector.get_columns("job_attempts")}
        for column in (
            "job_id",
            "attempt_number",
            "status",
            "worker_id",
            "claim_token",
            "claimed_at",
            "lease_expires_at",
            "last_heartbeat_at",
            "created_at",
        ):
            assert attempts[column]["nullable"] is False, column
        for column in ("execution_started_at", "finished_at", "retry_disposition"):
            assert attempts[column]["nullable"] is True, column
        disposition_type = attempts["retry_disposition"]["type"]
        assert isinstance(disposition_type, String)
        assert disposition_type.length == 20

        events = {column["name"]: column for column in inspector.get_columns("run_events")}
        for column in (
            "run_id",
            "job_id",
            "sequence",
            "event_type",
            "created_at",
        ):
            assert events[column]["nullable"] is False, column
        for column in ("attempt_id", "code", "message", "attempt_number", "available_at"):
            assert events[column]["nullable"] is True, column

        assert {item["name"] for item in inspector.get_unique_constraints("jobs")} == {
            "uq_jobs_run_id"
        }
        assert {item["name"] for item in inspector.get_unique_constraints("job_attempts")} == {
            "uq_job_attempts_job_id"
        }
        assert {item["name"] for item in inspector.get_unique_constraints("run_events")} == {
            "uq_run_events_run_id"
        }

        jobs_foreign_keys = {item["name"]: item for item in inspector.get_foreign_keys("jobs")}
        assert set(jobs_foreign_keys) == {
            "fk_jobs_run_id_runs",
            "fk_jobs_agent_instance_id_agent_instances",
        }
        attempts_foreign_keys = inspector.get_foreign_keys("job_attempts")
        assert [item["name"] for item in attempts_foreign_keys] == ["fk_job_attempts_job_id_jobs"]
        events_foreign_keys = {
            item["name"]: item for item in inspector.get_foreign_keys("run_events")
        }
        assert set(events_foreign_keys) == {
            "fk_run_events_run_id_runs",
            "fk_run_events_job_id_jobs",
            "fk_run_events_attempt_id_job_attempts",
        }
        all_foreign_keys = (
            *jobs_foreign_keys.values(),
            *attempts_foreign_keys,
            *events_foreign_keys.values(),
        )
        for foreign_key in all_foreign_keys:
            options = foreign_key.get("options") or {}
            assert options.get("ondelete") == "RESTRICT"

        assert {item["name"] for item in inspector.get_indexes("jobs")} == {
            "ix_jobs_status_available_at_id",
            "ix_jobs_agent_instance_id_status",
            "ix_jobs_status_lease_expires_at_id",
        }
        assert {item["name"] for item in inspector.get_indexes("job_attempts")} == {
            "ix_job_attempts_status_lease_expires_at_id",
            "uq_job_attempts_one_active",
        }
        assert {item["name"] for item in inspector.get_indexes("run_events")} == {
            "ix_run_events_attempt_id_id"
        }

        with engine.connect() as connection:
            active_index = connection.scalar(
                text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='index' AND name='uq_job_attempts_one_active'"
                )
            )
        assert normalize_sql(str(active_index)) == normalize_sql(
            "CREATE UNIQUE INDEX uq_job_attempts_one_active ON job_attempts (job_id) "
            "WHERE status IN ('claimed','running')"
        )
    finally:
        engine.dispose()


def test_migrated_c1_job_shapes_match_the_durable_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every legal C1/C2 Job state persists; every illegal shape is rejected by SQLite."""
    _, engine = migrate_database(tmp_path / "c1-jobs.db", monkeypatch)
    try:
        seed_c1_parents(engine, run_count=7)
        claimed = {
            **queued_job(),
            "run_id": 2,
            "status": "claimed",
            "attempt_count": 1,
            "claimed_by": "worker-1",
            "claim_token": b"c" * 32,
            "lease_expires_at": "2026-01-01 00:01:00",
            "last_heartbeat_at": "2026-01-01 00:00:00",
        }
        for row in (
            {**queued_job(), "run_id": 1},
            claimed,
            {**claimed, "run_id": 3, "status": "running"},
            {
                **queued_job(),
                "run_id": 4,
                "status": "retry_wait",
                "attempt_count": 1,
                "available_at": "2026-01-01 00:00:10",
            },
            {
                **queued_job(),
                "run_id": 5,
                "status": "succeeded",
                "attempt_count": 1,
                "finished_at": "2026-01-01 00:00:02",
                "updated_at": "2026-01-01 00:00:02",
            },
            {
                **queued_job(),
                "run_id": 6,
                "status": "cancelled",
                "attempt_count": 1,
                "cancel_requested_at": "2026-01-01 00:00:01",
                "finished_at": "2026-01-01 00:00:03",
                "updated_at": "2026-01-01 00:00:03",
                "error_code": "run_cancelled",
                "error_message": "cancelled",
            },
        ):
            insert_row(engine, "jobs", row)

        invalid_jobs = [
            {**queued_job(), "run_id": 7, "finished_at": "2026-01-01 00:00:01"},
            {**queued_job(), "run_id": 7, "status": "succeeded"},
            {
                **queued_job(),
                "run_id": 7,
                "status": "succeeded",
                "finished_at": "2026-01-01 00:00:01",
                "error_code": "model_error",
                "error_message": "safe",
            },
            {**queued_job(), "run_id": 7, "status": "failed", "finished_at": "2026-01-01 00:00:01"},
            {
                **queued_job(),
                "run_id": 7,
                "status": "cancelled",
                "finished_at": "2026-01-01 00:00:01",
                "error_code": "run_cancelled",
                "error_message": "cancelled",
            },
            {**queued_job(), "run_id": 7, "claimed_by": "worker-1"},
            {**claimed, "run_id": 7, "claimed_by": "w" * 129},
            {**claimed, "run_id": 7, "claim_token": b"c" * 31},
            {**queued_job(), "run_id": 7, "available_at": "2025-12-31 23:59:59"},
            {**queued_job(), "run_id": 7, "model_provider": ""},
            {
                **queued_job(),
                "run_id": 7,
                "status": "failed",
                "finished_at": "2026-01-01 00:00:01",
                "error_code": "e" * 65,
                "error_message": "safe",
            },
        ]
        for values in invalid_jobs:
            assert_integrity_error(engine, "jobs", values)
    finally:
        engine.dispose()


def test_migrated_c1_attempt_shapes_and_single_active_attempt_are_enforced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attempt history, the one-active-Attempt rule, and retry dispositions are enforced."""
    _, engine = migrate_database(tmp_path / "c1-attempts.db", monkeypatch)
    try:
        seed_c1_parents(engine, run_count=3)
        for run_id in (1, 2, 3):
            insert_row(engine, "jobs", {**queued_job(), "run_id": run_id})
        insert_row(engine, "job_attempts", {**claimed_attempt(), "job_id": 1})
        insert_row(
            engine,
            "job_attempts",
            {
                **claimed_attempt(),
                "job_id": 2,
                "status": "running",
                "execution_started_at": "2026-01-01 00:00:01",
            },
        )
        # Several terminal Attempts, including started ones, are legal history.
        for row in (
            {
                "attempt_number": 1,
                "status": "succeeded",
                "finished_at": "2026-01-01 00:00:02",
            },
            {
                "attempt_number": 2,
                "status": "failed",
                "finished_at": "2026-01-01 00:00:03",
                "retry_disposition": "SAFE_TO_RETRY",
                "error_code": "model_rate_limited",
                "error_message": "rate limited",
            },
            {
                "attempt_number": 3,
                "status": "expired",
                "finished_at": "2026-01-01 00:00:04",
                "retry_disposition": "AMBIGUOUS",
                "error_code": "execution_outcome_ambiguous",
                "error_message": "ambiguous",
            },
            {"attempt_number": 4, "status": "cancelled", "finished_at": "2026-01-01 00:00:05"},
        ):
            insert_row(
                engine,
                "job_attempts",
                {
                    **claimed_attempt(),
                    "job_id": 3,
                    "execution_started_at": "2026-01-01 00:00:01",
                    **row,
                },
            )

        # Job 1 already owns an active Attempt: a second active Attempt must be rejected.
        assert_integrity_error(
            engine, "job_attempts", {**claimed_attempt(), "job_id": 1, "attempt_number": 2}
        )

        invalid_attempts = [
            {**claimed_attempt(), "job_id": 3, "attempt_number": 9, "worker_id": ""},
            {**claimed_attempt(), "job_id": 3, "attempt_number": 9, "claim_token": b"a" * 31},
            {
                **claimed_attempt(),
                "job_id": 3,
                "attempt_number": 9,
                "created_at": "2025-12-31 23:59:59",
            },
            {
                **claimed_attempt(),
                "job_id": 3,
                "attempt_number": 9,
                "lease_expires_at": "2026-01-01 00:00:00",
            },
            {
                **claimed_attempt(),
                "job_id": 3,
                "attempt_number": 9,
                "execution_started_at": "2025-12-31 23:59:59",
            },
            {
                **claimed_attempt(),
                "job_id": 3,
                "attempt_number": 9,
                "status": "succeeded",
                "execution_started_at": "2026-01-01 00:00:10",
                "finished_at": "2026-01-01 00:00:05",
            },
            {
                **claimed_attempt(),
                "job_id": 3,
                "attempt_number": 9,
                "execution_started_at": "2026-01-01 00:00:01",
            },
            {**claimed_attempt(), "job_id": 3, "attempt_number": 9, "status": "running"},
            {
                **claimed_attempt(),
                "job_id": 3,
                "attempt_number": 9,
                "finished_at": "2026-01-01 00:00:02",
            },
            {
                **claimed_attempt(),
                "job_id": 3,
                "attempt_number": 9,
                "retry_disposition": "AMBIGUOUS",
            },
            {
                **claimed_attempt(),
                "job_id": 3,
                "attempt_number": 9,
                "status": "failed",
                "finished_at": "2026-01-01 00:00:02",
            },
            {
                **claimed_attempt(),
                "job_id": 3,
                "attempt_number": 9,
                "status": "succeeded",
                "finished_at": "2026-01-01 00:00:02",
                "retry_disposition": "SAFE_TO_RETRY",
            },
            {
                **claimed_attempt(),
                "job_id": 3,
                "attempt_number": 9,
                "status": "failed",
                "finished_at": "2026-01-01 00:00:02",
                "retry_disposition": "MAYBE",
            },
            {
                **claimed_attempt(),
                "job_id": 3,
                "attempt_number": 9,
                "status": "failed",
                "finished_at": "2026-01-01 00:00:02",
                "retry_disposition": "DO_NOT_RETRY",
                "error_code": "e" * 65,
                "error_message": "safe",
            },
            {**claimed_attempt(), "job_id": 3, "attempt_number": 0},
        ]
        for values in invalid_attempts:
            assert_integrity_error(engine, "job_attempts", values)
    finally:
        engine.dispose()


def test_migrated_c1_run_event_vocabulary_and_ordering_are_enforced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run Events stay an append-only, allowlisted, per-Run ordered typed stream."""
    _, engine = migrate_database(tmp_path / "c1-events.db", monkeypatch)
    try:
        seed_c1_parents(engine, run_count=1)
        insert_row(engine, "jobs", queued_job())
        created = {
            "run_id": 1,
            "job_id": 1,
            "attempt_id": None,
            "sequence": 1,
            "event_type": "run.created",
            "code": None,
            "message": None,
            "attempt_number": None,
            "available_at": None,
            "created_at": "2026-01-01 00:00:00",
        }
        for row in (
            created,
            {
                **created,
                "sequence": 2,
                "event_type": "run.queued",
                "available_at": "2026-01-01 00:00:00",
            },
            {
                **created,
                "sequence": 3,
                "event_type": "retry.scheduled",
                "available_at": "2026-01-01 00:00:10",
            },
            {
                **created,
                "sequence": 4,
                "event_type": "recovery.ambiguous",
                "code": "execution_outcome_ambiguous",
                "message": "ambiguous",
                "attempt_number": 1,
            },
        ):
            insert_row(engine, "run_events", row)

        invalid_events = [
            {**created, "sequence": 9, "event_type": "run.exploded"},
            {**created, "sequence": 9, "event_type": "heartbeat"},
            {**created, "sequence": 9, "event_type": "retry.scheduled"},
            {**created, "sequence": 9, "code": "model_error"},
            {**created, "sequence": 9, "code": "model_error", "message": "m" * 513},
            {**created, "sequence": 0},
            {**created, "sequence": -1},
            {**created, "sequence": 9, "attempt_number": 0},
            created,
        ]
        for values in invalid_events:
            assert_integrity_error(engine, "run_events", values)
    finally:
        engine.dispose()


# -- C5 cancellation: the fifth Run lifecycle -------------------------------------------


def test_migrated_c5_cancelled_run_shapes_are_enforced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two cancelled shapes persist; every illegal cancelled shape is rejected."""
    _, engine = migrate_database(tmp_path / "c5-cancelled.db", monkeypatch)
    try:
        seed_c1_parents(engine, run_count=0)
        pre_start = {
            **snapshot_run(),
            "status": "cancelled",
            "finished_at": "2026-01-01 00:00:05",
        }
        post_start = {
            **pre_start,
            "started_at": "2026-01-01 00:00:01",
            "elapsed_ms": 4000,
        }
        # Both legal shapes really persist.
        insert_row(engine, "runs", {**pre_start, "id": 1})
        insert_row(engine, "runs", {**post_start, "id": 2})
        # A cancelled Run is not a failure: it may never carry a provider error, output, or usage.
        illegal = [
            {**pre_start, "error_code": "model_unavailable", "error_message": "broke"},
            {**pre_start, "output_text": "leaked"},
            {**pre_start, "finish_reason": "stop"},
            {**pre_start, "input_tokens": 3},
            {**pre_start, "total_tokens": 3},
            # The duration is tied to the start boundary in both directions.
            {**pre_start, "elapsed_ms": 0},
            {**post_start, "elapsed_ms": None},
            # And a cancelled Run still has to finish after it started.
            {**post_start, "finished_at": "2025-12-31 23:59:59"},
        ]
        for values in illegal:
            assert_integrity_error(engine, "runs", {**values, "id": 3})
    finally:
        engine.dispose()


def test_every_earlier_run_shape_still_persists_after_0005(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C5 widened the vocabulary; it did not disturb the four shapes C1-C4 already stored."""
    _, engine = migrate_database(tmp_path / "c5-regression.db", monkeypatch)
    try:
        seed_c1_parents(engine, run_count=0)
        succeeded = {
            **snapshot_run(),
            "status": "succeeded",
            "started_at": "2026-01-01 00:00:01",
            "finished_at": "2026-01-01 00:00:02",
            "output_text": "answer",
            "finish_reason": "stop",
            "elapsed_ms": 1000,
        }
        failed = {
            **snapshot_run(),
            "status": "failed",
            "started_at": "2026-01-01 00:00:01",
            "finished_at": "2026-01-01 00:00:02",
            "error_code": "model_unavailable",
            "error_message": "safe message",
            "elapsed_ms": 1000,
        }
        exhausted = {
            **snapshot_run(),
            "status": "failed",
            "finished_at": "2026-01-01 00:00:02",
            "error_code": "worker_recovery_exhausted",
            "error_message": "safe message",
        }
        for index, values in enumerate((snapshot_run(), succeeded, failed, exhausted), start=1):
            insert_row(engine, "runs", {**values, "id": index})
    finally:
        engine.dispose()


def test_the_c5_downgrade_refuses_to_strand_a_cancelled_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0004 cannot represent `cancelled`, so downgrade refuses rather than rewriting history."""
    database_path = tmp_path / "c5-downgrade.db"
    config = alembic_config(database_path, monkeypatch)
    command.upgrade(config, "head")
    engine = create_sqlite_engine(database_path)
    try:
        seed_c1_parents(engine, run_count=0)
        insert_row(
            engine,
            "runs",
            {
                **snapshot_run(),
                "status": "cancelled",
                "finished_at": "2026-01-01 00:00:05",
            },
        )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="cancelled Run"):
        command.downgrade(config, "0004_stage_c3_worker_registry")

    # Nothing was destroyed by the refusal: the cancelled Run is still there and still cancelled.
    engine = create_sqlite_engine(database_path)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT status FROM runs WHERE id=1")) == "cancelled"
    finally:
        engine.dispose()


def test_the_c5_downgrade_is_clean_when_nothing_needs_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "c5-clean-downgrade.db"
    config = alembic_config(database_path, monkeypatch)
    command.upgrade(config, "head")
    engine = create_sqlite_engine(database_path)
    try:
        # A Run that no cancellation touched must survive the round trip untouched.
        seed_c1_parents(engine, run_count=1)
    finally:
        engine.dispose()

    command.downgrade(config, "0004_stage_c3_worker_registry")
    command.upgrade(config, "head")

    engine = create_sqlite_engine(database_path)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM runs")) == 1
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0006_stage_c6_queue_partitions"
            )
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# 0006 -- the C6 queue-partition fairness metadata
# ---------------------------------------------------------------------------------------

C6_TABLES = ("queue_partitions",)
EXECUTION_TABLES = ("runs", "jobs", "job_attempts", "run_events", "workers")


def execution_snapshot(engine: Engine) -> dict[str, list[tuple[Any, ...]]]:
    """Return every execution row, ordered stably, so 0006 can be proven not to rewrite any."""
    snapshot: dict[str, list[tuple[Any, ...]]] = {}
    with engine.connect() as connection:
        for table in EXECUTION_TABLES:
            rows = connection.exec_driver_sql(f"SELECT * FROM {table}").fetchall()
            snapshot[table] = [tuple(row) for row in rows]
    return snapshot


def partition_markers(engine: Engine) -> dict[int, int | None]:
    with engine.connect() as connection:
        return {
            int(row[0]): None if row[1] is None else int(row[1])
            for row in connection.exec_driver_sql(
                "SELECT agent_instance_id, last_served_attempt_id FROM queue_partitions"
            ).fetchall()
        }


def test_migration_0006_creates_only_the_fairness_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0006 adds scheduling metadata and touches nothing else."""
    database_path = tmp_path / "c6-surface.db"
    config = alembic_config(database_path, monkeypatch)
    command.upgrade(config, "0005_stage_c5_run_cancellation")
    engine = create_sqlite_engine(database_path)
    try:
        assert "queue_partitions" not in application_tables(engine)
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_sqlite_engine(database_path)
    try:
        assert application_tables(engine) == APPLICATION_TABLES
        assert "queue_partitions" in application_tables(engine)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0006_stage_c6_queue_partitions"
            )
            columns = [
                str(row[1])
                for row in connection.exec_driver_sql(
                    "PRAGMA table_info(queue_partitions)"
                ).fetchall()
            ]
        assert columns == ["agent_instance_id", "last_served_attempt_id"]
    finally:
        engine.dispose()


def test_migration_0006_backfills_one_marker_per_served_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backfill is deterministic and reads only Attempt ids that already exist."""
    database_path = tmp_path / "c6-backfill.db"
    config = alembic_config(database_path, monkeypatch)
    command.upgrade(config, "0005_stage_c5_run_cancellation")
    engine = create_sqlite_engine(database_path)
    try:
        seed_c1_parents(engine, run_count=2)
        insert_row(engine, "jobs", queued_job())
        insert_row(engine, "job_attempts", claimed_attempt())
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_sqlite_engine(database_path)
    try:
        # Agent 1 has one Attempt, so its marker is that Attempt's id.
        assert partition_markers(engine) == {1: 1}
    finally:
        engine.dispose()


def test_migration_0006_backfills_a_null_marker_for_a_never_claimed_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "c6-never-claimed.db"
    config = alembic_config(database_path, monkeypatch)
    command.upgrade(config, "0005_stage_c5_run_cancellation")
    engine = create_sqlite_engine(database_path)
    try:
        seed_c1_parents(engine, run_count=1)
        insert_row(engine, "jobs", queued_job())
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_sqlite_engine(database_path)
    try:
        assert partition_markers(engine) == {1: None}
    finally:
        engine.dispose()


def test_migration_0006_rewrites_no_execution_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "c6-no-rewrite.db"
    config = alembic_config(database_path, monkeypatch)
    command.upgrade(config, "0005_stage_c5_run_cancellation")
    engine = create_sqlite_engine(database_path)
    try:
        seed_c1_parents(engine, run_count=2)
        insert_row(engine, "jobs", queued_job())
        insert_row(engine, "job_attempts", claimed_attempt())
        before = execution_snapshot(engine)
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_sqlite_engine(database_path)
    try:
        assert execution_snapshot(engine) == before
    finally:
        engine.dispose()


def test_the_c6_downgrade_drops_metadata_and_reconstructs_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The metadata is derived, so 0006 downgrades cleanly instead of refusing.

    Unlike 0004 and 0005, dropping this table rewrites no history: re-upgrading restores the
    identical markers from the Attempt rows the migration never modifies.
    """
    database_path = tmp_path / "c6-downgrade.db"
    config = alembic_config(database_path, monkeypatch)
    command.upgrade(config, "head")
    engine = create_sqlite_engine(database_path)
    try:
        seed_c1_parents(engine, run_count=2)
        insert_row(engine, "jobs", queued_job())
        insert_row(engine, "job_attempts", claimed_attempt())
        insert_row(
            engine, "queue_partitions", {"agent_instance_id": 1, "last_served_attempt_id": 1}
        )
        markers = partition_markers(engine)
        history = execution_snapshot(engine)
    finally:
        engine.dispose()
    assert markers == {1: 1}

    command.downgrade(config, "0005_stage_c5_run_cancellation")
    engine = create_sqlite_engine(database_path)
    try:
        assert "queue_partitions" not in application_tables(engine)
        # No execution row was touched by the downgrade.
        assert execution_snapshot(engine) == history
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_sqlite_engine(database_path)
    try:
        assert application_tables(engine) == APPLICATION_TABLES
        assert partition_markers(engine) == markers
        assert execution_snapshot(engine) == history
    finally:
        engine.dispose()
