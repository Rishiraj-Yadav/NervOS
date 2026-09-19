"""Schema revision readiness shared by process composition roots."""

from __future__ import annotations

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

SCHEMA_MIGRATION_HINT = (
    "Run `uv run alembic -c apps/api/alembic.ini upgrade head` before starting this process. "
    "A process validates the applied revision and refuses to start against any other; none of "
    "them migrates the database itself."
)


class SchemaRevisionMismatch(RuntimeError):
    """The database schema is not at the revision a process requires."""


def read_schema_revision(engine: Engine) -> str | None:
    """Read the applied Alembic revision, or None when the database is uninitialized."""
    try:
        with engine.connect() as connection:
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
    except SQLAlchemyError:
        return None
    return None if revision is None else str(revision)


def require_schema_revision(engine: Engine, expected: str) -> str:
    """Fail loudly rather than executing against an unknown schema."""
    revision = read_schema_revision(engine)
    if revision is None or revision != expected:
        raise SchemaRevisionMismatch(
            f"database schema revision is {revision!r}, expected {expected!r}. "
            f"{SCHEMA_MIGRATION_HINT}"
        )
    return revision
