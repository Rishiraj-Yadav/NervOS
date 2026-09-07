"""Synchronous SQLite engine and session construction."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry


def build_sqlite_url(database_path: Path) -> URL:
    """Build a SQLite URL without manually quoting a filesystem path."""
    return URL.create(drivername="sqlite+pysqlite", database=str(database_path))


def _configure_sqlite_connection(
    dbapi_connection: Any,
    connection_record: ConnectionPoolEntry,
) -> None:
    """Apply required connection-local SQLite settings."""
    del connection_record
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def create_sqlite_engine(database_path: Path) -> Engine:
    """Create a synchronous SQLite engine with required connection PRAGMAs."""
    engine = create_engine(
        build_sqlite_url(database_path),
        connect_args={"check_same_thread": False, "timeout": 5.0},
    )
    event.listen(engine, "connect", _configure_sqlite_connection)
    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create sessions whose transaction boundaries are owned by callers."""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
