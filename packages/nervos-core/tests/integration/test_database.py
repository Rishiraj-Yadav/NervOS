"""Integration tests for synchronous SQLite infrastructure."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from nervos_core.infrastructure.database import (
    UTCDateTime,
    build_sqlite_url,
    create_session_factory,
    create_sqlite_engine,
)
from sqlalchemy import Column, Integer, MetaData, Table, insert, select, text
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import Session


def test_windows_safe_url_opens_exact_special_path(tmp_path: Path) -> None:
    database_path = tmp_path / "space # percent % database.db"
    engine = create_sqlite_engine(database_path)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT 1")) == 1
        assert database_path.is_file()
        assert engine.url == build_sqlite_url(database_path)
        assert engine.url.drivername == "sqlite+pysqlite"
        assert engine.url.database == str(database_path)
    finally:
        engine.dispose()


def test_every_connection_receives_required_pragmas(tmp_path: Path) -> None:
    engine = create_sqlite_engine(tmp_path / "pragmas.db")
    try:
        for _ in range(2):
            with engine.connect() as connection:
                assert connection.scalar(text("PRAGMA foreign_keys")) == 1
                assert connection.scalar(text("PRAGMA busy_timeout")) == 5000
                assert connection.scalar(text("PRAGMA journal_mode")) != "wal"
            engine.dispose()
    finally:
        engine.dispose()


def test_session_factory_uses_explicit_transactions(tmp_path: Path) -> None:
    engine = create_sqlite_engine(tmp_path / "sessions.db")
    factory = create_session_factory(engine)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE values_table (value INTEGER NOT NULL)"))

        session = factory()
        session.execute(text("INSERT INTO values_table (value) VALUES (1)"))
        session.rollback()
        session.close()

        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM values_table")) == 0

        with factory.begin() as committed_session:
            committed_session.execute(text("INSERT INTO values_table (value) VALUES (2)"))

        with engine.connect() as connection:
            assert connection.scalar(text("SELECT value FROM values_table")) == 2
    finally:
        engine.dispose()


def test_utc_datetime_round_trip_and_naive_rejection(tmp_path: Path) -> None:
    engine = create_sqlite_engine(tmp_path / "datetimes.db")
    metadata = MetaData()
    timestamps = Table(
        "timestamps",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("value", UTCDateTime(), nullable=False),
        Column("optional_value", UTCDateTime(), nullable=True),
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE timestamps ("
                    "id INTEGER PRIMARY KEY, value DATETIME NOT NULL, optional_value DATETIME)"
                )
            )

        expected = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
        with engine.begin() as connection:
            connection.execute(insert(timestamps).values(id=1, value=expected, optional_value=None))

        with engine.connect() as connection:
            row = connection.execute(select(timestamps)).one()
            assert row.value == expected
            assert row.value.tzinfo is UTC
            assert row.optional_value is None

        session = Session(engine)
        try:
            with pytest.raises(StatementError, match="timezone-aware"):
                session.execute(
                    insert(timestamps).values(
                        id=2,
                        value=datetime(2026, 3, 4, 5, 6, 7),
                    )
                )
                session.flush()
            session.rollback()
        finally:
            session.close()
    finally:
        engine.dispose()
