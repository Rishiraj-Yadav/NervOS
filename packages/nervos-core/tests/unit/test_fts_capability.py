"""Unit tests for FTS5 capability probe and non-authoritative search isolation."""

from __future__ import annotations

import sqlite3


def test_fts5_runtime_capability_probe() -> None:
    """Verify standard library SQLite build supports FTS5 for search extensions."""
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE test_fts USING fts5(content);")
        connection.execute(
            "INSERT INTO test_fts(content) VALUES ('NervOS scoped memory retrieval');"
        )
        rows = connection.execute("SELECT * FROM test_fts WHERE test_fts MATCH 'NervOS'").fetchall()
        assert len(rows) == 1
        assert "scoped memory" in rows[0][0]
    finally:
        connection.close()
