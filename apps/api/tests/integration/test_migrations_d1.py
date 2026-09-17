"""D1 (0007) migration lifecycle: upgrade, and the refusal that protects durable evidence.

The refusal matters more than the convenience. Dropping populated Stage D state would destroy
durable evidence -- including the per-Attempt token accounting, which has no pre-0007
representation at all -- so every preflight condition is evaluated *before* the first destructive
statement. A refused downgrade therefore leaves the schema and the data exactly as they were; it
never partially downgrades, never deletes history to succeed, and never rewrites a Run Event or a
ToolInvocation to fabricate compatibility.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from nervos_api.config import Settings
from nervos_core.infrastructure.database import create_sqlite_engine
from sqlalchemy import Engine, inspect, text

ROOT = Path(__file__).resolve().parents[4]
ALEMBIC_INI = ROOT / "apps" / "api" / "alembic.ini"
VERSIONS = ROOT / "apps" / "api" / "alembic" / "versions"
DEFAULT_DATABASE = (Path.home() / ".nervos" / "nervos.db").resolve(strict=False)

D1_REVISION = "0007_stage_d1_tool_capability_audit"
C6_REVISION = "0006_stage_c6_queue_partitions"

# The exact content of every migration D1 builds on. D1 adds 0007 and rewrites nothing, and a
# frozen-history claim is only worth making if something fails when it stops being true.
#
# The hashes are over line-ending-normalised bytes, not raw checkout bytes. A raw hash would make
# the frozen claim depend on the machine's `core.autocrlf`: a Windows checkout with CRLF produces a
# different digest than the LF bytes every other environment reads, so the same unchanged file
# would pass in one place and fail in another. Normalising keeps the guard about *content*, which is
# what "rewrites nothing" actually means.
FROZEN_MIGRATIONS = (
    ("0001_stage_a_schema.py", "7d7117616bfe1266309417ab427fba70c1e6690663ff18924c64794460a06f0b"),
    (
        "0002_stage_b1_agent_instances_runs.py",
        "4fa78a579fd4a334a9262e74ef688eaf0c93241dd450962cdfb77fd2487f1044",
    ),
    (
        "0003_stage_c1_durable_execution.py",
        "b4f4ddf8c1edb8e8666422fe8b795016e420286a8a4bbf8e987e1c566cbcbd4f",
    ),
    (
        "0004_stage_c3_worker_registry.py",
        "aed42996824451e8f986fbbcd0d84c77c446012a3783e83f7a3ecf8ea58d863b",
    ),
    (
        "0005_stage_c5_run_cancellation.py",
        "fa40c802febb87719fde1f4b62ef9784282cf45b8cfef88b532b87900457e336",
    ),
    (
        "0006_stage_c6_queue_partitions.py",
        "2827ed7cdfcbb2520204040999b3bed6f5d59e6f8d9a293bd9d894ab09f639c7",
    ),
)


def frozen_digest(path: Path) -> str:
    """Hash a migration's content, independent of the checkout's line endings."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()

NOW = datetime(2026, 9, 17, tzinfo=UTC)
# An unexpired lease: `lease_expires_at > claimed_at` is a C2 invariant, not an incidental value.
LEASE = NOW + timedelta(minutes=1)
FINGERPRINT = "a" * 64

STAGE_D_TABLES = (
    "mcp_connections",
    "tool_definitions",
    "agent_tool_grants",
    "tool_invocations",
)


def database_fingerprint(path: Path) -> tuple[bool, int, int]:
    """Identify a database file without reading any of its contents."""
    if not path.exists():
        return (False, 0, 0)
    stat = path.stat()
    return (True, stat.st_size, stat.st_mtime_ns)


@pytest.fixture(autouse=True)
def default_database_is_untouched() -> Iterator[None]:
    """Prove every D1 migration test works on a disposable database, never the developer's."""
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


def migrate_to_head(database_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Config, Engine]:
    """Upgrade a disposable database to the D1 head, with one owner and one Agent Instance.

    The owner rows are seeded because almost every Stage D row hangs off them by RESTRICT foreign
    key, and because a migration test that silently forgot them would fail for the wrong reason.
    """
    config = alembic_config(database_path, monkeypatch)
    command.upgrade(config, "head")
    engine = create_sqlite_engine(database_path)
    seed_owner(engine)
    return config, engine


def seed_owner(engine: Engine) -> None:
    """Insert the user and Agent Instance every Stage D row ultimately belongs to."""
    insert_row(
        engine,
        "users",
        {
            "id": 1,
            "username": "user1",
            "password_hash": "h",
            "role": "admin",
            "is_active": 1,
            "created_at": NOW,
            "updated_at": NOW,
        },
    )
    insert_row(
        engine,
        "agent_instances",
        {
            "id": 1,
            "owner_user_id": 1,
            "agent_key": "nervos.chat",
            "agent_definition_version": "1",
            "display_name": "Chat",
            "enabled": 1,
            "model_provider": "anthropic",
            "model_name": "opaque/model",
            "created_at": NOW,
            "updated_at": NOW,
        },
    )


def insert_row(engine: Engine, table_name: str, values: dict[str, Any]) -> None:
    """Insert one test row using named parameters."""
    columns = ", ".join(values)
    parameters = ", ".join(f":{column}" for column in values)
    with engine.begin() as connection:
        connection.execute(
            text(f"INSERT INTO {table_name} ({columns}) VALUES ({parameters})"), values
        )


def scalar(engine: Engine, statement: str, **values: object) -> Any:
    with engine.connect() as connection:
        return connection.scalar(text(statement), values)


def seed_run(engine: Engine, *, run_id: int = 1, **overrides: object) -> None:
    """Insert one `created` Run, the shape C2 submits."""
    values: dict[str, object] = {
        "id": run_id,
        "agent_instance_id": 1,
        "status": "created",
        "agent_key": "nervos.chat",
        "agent_definition_version": "1",
        "model_provider": "anthropic",
        "model_name": "opaque/model",
        "input_text": "hello",
        "input_max_bytes": 8000,
        "input_max_code_points": 4000,
        "output_max_bytes": 32000,
        "output_max_code_points": 16000,
        "provider_timeout_ms": 60000,
        "max_output_tokens": 1024,
        "max_model_calls": 1,
        "created_at": NOW,
    }
    values.update(overrides)
    insert_row(engine, "runs", values)


def seed_job(engine: Engine, *, job_id: int = 1, run_id: int = 1) -> None:
    insert_row(
        engine,
        "jobs",
        {
            "id": job_id,
            "run_id": run_id,
            "agent_instance_id": 1,
            "model_provider": "anthropic",
            "status": "queued",
            "available_at": NOW,
            "attempt_count": 0,
            "max_attempts": 3,
            "created_at": NOW,
            "updated_at": NOW,
        },
    )


def seed_attempt(engine: Engine, *, attempt_id: int = 1, job_id: int = 1) -> None:
    insert_row(
        engine,
        "job_attempts",
        {
            "id": attempt_id,
            "job_id": job_id,
            "attempt_number": 1,
            "status": "running",
            "worker_id": "worker-1",
            "claim_token": b"a" * 32,
            "claimed_at": NOW,
            "execution_started_at": NOW,
            "lease_expires_at": LEASE,
            "last_heartbeat_at": NOW,
            "created_at": NOW,
        },
    )


def seed_parents(engine: Engine) -> None:
    """Create the Run, Job and Attempt every ToolInvocation must reference."""
    seed_run(engine)
    seed_job(engine)
    seed_attempt(engine)


def connection_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "owner_user_id": 1,
        "display_name": "GitHub",
        "transport": "http",
        "endpoint": "https://mcp.example.test/rpc",
        "catalog_status": "unavailable",
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(overrides)
    return row


def definition_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "source_kind": "builtin",
        "source_id": None,
        "upstream_name": "current_time",
        "model_name": "nervos__builtin__current_time",
        "display_name": "Current time",
        "description": "Return the current time.",
        "input_schema": "{}",
        "fingerprint": FINGERPRINT,
        "status": "available",
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(overrides)
    return row


def grant_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "agent_instance_id": 1,
        "tool_definition_id": 1,
        "reviewed_fingerprint": FINGERPRINT,
        "created_at": NOW,
    }
    row.update(overrides)
    return row


def invocation_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "run_id": 1,
        "job_id": 1,
        "attempt_id": 1,
        "tool_sequence": 1,
        "tool_definition_id": 1,
        "source_kind": "builtin",
        "source_id": None,
        "upstream_name": "current_time",
        "model_name": "nervos__builtin__current_time",
        "definition_fingerprint": FINGERPRINT,
        "status": "requested",
        "permission_decision": "allowed",
        "requested_at": NOW,
    }
    row.update(overrides)
    return row


def assert_refused_and_intact(database_path: Path, config: Config, blocker: str) -> None:
    """Refuse the downgrade, then prove nothing was partially destroyed.

    This is the half of the contract that a bare `pytest.raises` would miss: an exception is only
    acceptable if the durable state and the schema are exactly where they were.
    """
    with pytest.raises(RuntimeError) as failure:
        command.downgrade(config, C6_REVISION)
    assert blocker in str(failure.value)

    engine = create_sqlite_engine(database_path)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        for table in STAGE_D_TABLES:
            assert table in tables, table
        assert "tool_grant_cutoff_id" in {c["name"] for c in inspector.get_columns("runs")}
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                D1_REVISION
            )
            assert connection.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------------------


def test_d1_rewrites_no_earlier_migration() -> None:
    """0001 through 0006 are byte-for-byte identical; D1 adds 0007 and rewrites nothing."""
    for filename, expected_hash in FROZEN_MIGRATIONS:
        assert frozen_digest(VERSIONS / filename) == expected_hash, filename


def test_the_d1_upgrade_creates_the_four_tables_and_extends_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "d1-surface.db"
    config = alembic_config(database_path, monkeypatch)
    command.upgrade(config, C6_REVISION)
    engine = create_sqlite_engine(database_path)
    try:
        inspector = inspect(engine)
        before = set(inspector.get_table_names())
        before_runs = {column["name"] for column in inspector.get_columns("runs")}
        before_attempts = {column["name"] for column in inspector.get_columns("job_attempts")}
        before_events = {column["name"] for column in inspector.get_columns("run_events")}
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_sqlite_engine(database_path)
    try:
        inspector = inspect(engine)
        after = set(inspector.get_table_names())
        assert after - before == set(STAGE_D_TABLES)
        assert {c["name"] for c in inspector.get_columns("runs")} - before_runs == {
            "max_tool_calls",
            "tool_timeout_ms",
            "tool_result_max_bytes",
            "max_consecutive_tool_failures",
            "tool_grant_cutoff_id",
        }
        assert {c["name"] for c in inspector.get_columns("job_attempts")} - before_attempts == {
            "input_tokens",
            "output_tokens",
            "total_tokens",
        }
        assert {c["name"] for c in inspector.get_columns("run_events")} - before_events == {
            "tool_invocation_id"
        }
        # No existing table lost a column to the two rebuilds.
        assert before_runs - {c["name"] for c in inspector.get_columns("runs")} == set()
        assert before_events - {c["name"] for c in inspector.get_columns("run_events")} == set()
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                D1_REVISION
            )
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    finally:
        engine.dispose()


def test_a_migrated_run_gains_no_tool_budget_and_no_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No grant is inferred for a historical Run: it has no budget and nothing admits a grant.

    This is the property that keeps the migration from silently widening what an already-started
    or already-finished Run was allowed to do.
    """
    database_path = tmp_path / "d1-run-defaults.db"
    config = alembic_config(database_path, monkeypatch)
    command.upgrade(config, C6_REVISION)
    engine = create_sqlite_engine(database_path)
    try:
        insert_row(
            engine,
            "users",
            {
                "id": 1,
                "username": "user1",
                "password_hash": "h",
                "role": "admin",
                "is_active": 1,
                "created_at": NOW,
                "updated_at": NOW,
            },
        )
        insert_row(
            engine,
            "agent_instances",
            {
                "id": 1,
                "owner_user_id": 1,
                "agent_key": "nervos.chat",
                "agent_definition_version": "1",
                "display_name": "Chat",
                "enabled": 1,
                "model_provider": "anthropic",
                "model_name": "opaque/model",
                "created_at": NOW,
                "updated_at": NOW,
            },
        )
        seed_run(engine)
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_sqlite_engine(database_path)
    try:
        row = (
            scalar(engine, "SELECT max_tool_calls FROM runs WHERE id=1"),
            scalar(engine, "SELECT tool_timeout_ms FROM runs WHERE id=1"),
            scalar(engine, "SELECT tool_result_max_bytes FROM runs WHERE id=1"),
            scalar(engine, "SELECT max_consecutive_tool_failures FROM runs WHERE id=1"),
            scalar(engine, "SELECT tool_grant_cutoff_id FROM runs WHERE id=1"),
        )
        assert row == (0, 30000, 65536, 3, 0)
    finally:
        engine.dispose()


def test_every_accepted_run_shape_survives_the_two_rebuilds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `runs` and `run_events` rebuilds must not weaken or lose an accepted lifecycle fact.

    All seven C1/C5 Run shapes are written at 0006, carried through the two table rebuilds, and
    read back unchanged -- including the `worker_recovery_exhausted` Run that has no start
    boundary and the two cancelled shapes.
    """
    database_path = tmp_path / "d1-run-shapes.db"
    config = alembic_config(database_path, monkeypatch)
    command.upgrade(config, C6_REVISION)
    engine = create_sqlite_engine(database_path)
    try:
        insert_row(
            engine,
            "users",
            {
                "id": 1,
                "username": "user1",
                "password_hash": "h",
                "role": "admin",
                "is_active": 1,
                "created_at": NOW,
                "updated_at": NOW,
            },
        )
        insert_row(
            engine,
            "agent_instances",
            {
                "id": 1,
                "owner_user_id": 1,
                "agent_key": "nervos.chat",
                "agent_definition_version": "1",
                "display_name": "Chat",
                "enabled": 1,
                "model_provider": "anthropic",
                "model_name": "opaque/model",
                "created_at": NOW,
                "updated_at": NOW,
            },
        )
        shapes = (
            {"status": "created"},
            {"status": "running", "started_at": NOW},
            {
                "status": "succeeded",
                "started_at": NOW,
                "finished_at": NOW,
                "output_text": "done",
                "elapsed_ms": 5,
            },
            {
                "status": "failed",
                "started_at": NOW,
                "finished_at": NOW,
                "error_code": "model_unavailable",
                "error_message": "provider unavailable",
                "elapsed_ms": 5,
            },
            {
                "status": "failed",
                "finished_at": NOW,
                "error_code": "worker_recovery_exhausted",
                "error_message": "attempt budget exhausted before execution",
            },
            {
                "status": "cancelled",
                "started_at": NOW,
                "finished_at": NOW,
                "elapsed_ms": 5,
            },
            {"status": "cancelled", "finished_at": NOW},
        )
        for index, shape in enumerate(shapes, start=1):
            seed_run(engine, run_id=index, **shape)
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_sqlite_engine(database_path)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT status, started_at, finished_at, output_text, error_code,"
                    " error_message, elapsed_ms, max_tool_calls, tool_grant_cutoff_id"
                    " FROM runs ORDER BY id"
                )
            ).fetchall()
        assert len(rows) == 7
        assert [row[0] for row in rows] == [
            "created",
            "running",
            "succeeded",
            "failed",
            "failed",
            "cancelled",
            "cancelled",
        ]
        # The Stage D columns arrive at their legacy values on every shape.
        assert {row[7] for row in rows} == {0}
        assert {row[8] for row in rows} == {0}
        # And the truth each shape carried is intact.
        assert rows[4][4] == "worker_recovery_exhausted"
        assert rows[4][1] is None
        assert rows[6][1] is None
        assert rows[2][3] == "done"
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# Downgrade refusal -- one condition per test
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("table", "row_factory", "blocker"),
    (
        ("mcp_connections", connection_row, "mcp_connections holds connection definitions"),
        ("tool_definitions", definition_row, "tool_definitions holds discovered definitions"),
        ("agent_tool_grants", grant_row, "agent_tool_grants holds capability grants"),
        ("tool_invocations", invocation_row, "tool_invocations holds tool-call audit rows"),
    ),
)
def test_the_downgrade_refuses_each_populated_stage_d_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    table: str,
    row_factory: Any,
    blocker: str,
) -> None:
    """One table at a time, so each refusal is attributable to the condition that caused it."""
    database_path = tmp_path / f"d1-refuse-{table}.db"
    config, engine = migrate_to_head(database_path, monkeypatch)
    try:
        seed_parents(engine)
        # A ToolInvocation needs a definition to point at, and the definition case is the row
        # under test -- so the prerequisite is only seeded for the other three.
        if table != "tool_definitions":
            insert_row(engine, "tool_definitions", definition_row())
        insert_row(engine, table, row_factory())
    finally:
        engine.dispose()

    assert_refused_and_intact(database_path, config, blocker)
    engine = create_sqlite_engine(database_path)
    try:
        assert scalar(engine, f"SELECT count(*) FROM {table}") == 1
    finally:
        engine.dispose()


def test_the_downgrade_refuses_a_stage_d_run_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "d1-refuse-event.db"
    config, engine = migrate_to_head(database_path, monkeypatch)
    try:
        seed_parents(engine)
        insert_row(
            engine,
            "run_events",
            {
                "run_id": 1,
                "job_id": 1,
                "sequence": 1,
                "event_type": "tool.requested",
                "created_at": NOW,
            },
        )
    finally:
        engine.dispose()

    assert_refused_and_intact(database_path, config, "run_events holds Stage D tool events")


def test_the_downgrade_refuses_an_event_that_references_an_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Distinct from the condition above: the *pointer* is Stage D-only, the event type is not."""
    database_path = tmp_path / "d1-refuse-event-pointer.db"
    config, engine = migrate_to_head(database_path, monkeypatch)
    try:
        seed_parents(engine)
        insert_row(engine, "tool_definitions", definition_row())
        insert_row(engine, "tool_invocations", invocation_row())
        insert_row(
            engine,
            "run_events",
            {
                "run_id": 1,
                "job_id": 1,
                "tool_invocation_id": 1,
                "sequence": 1,
                "event_type": "run.created",
                "created_at": NOW,
            },
        )
    finally:
        engine.dispose()

    assert_refused_and_intact(database_path, config, "run_events references a tool invocation")


def test_the_downgrade_refuses_a_non_zero_grant_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cutoff is the capability bound, so a Run holding one is not representable at 0006."""
    database_path = tmp_path / "d1-refuse-cutoff.db"
    config, engine = migrate_to_head(database_path, monkeypatch)
    try:
        seed_run(engine, tool_grant_cutoff_id=7)
    finally:
        engine.dispose()

    assert_refused_and_intact(database_path, config, "runs.tool_grant_cutoff_id differs")


@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("max_tool_calls", 4),
        ("tool_timeout_ms", 5000),
        ("tool_result_max_bytes", 2048),
        ("max_consecutive_tool_failures", 4),
    ),
)
def test_the_downgrade_refuses_a_non_default_run_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, column: str, value: int
) -> None:
    """Each value is legal at 0007 and still distinguishable from the legacy default."""
    database_path = tmp_path / f"d1-refuse-{column}.db"
    config, engine = migrate_to_head(database_path, monkeypatch)
    try:
        seed_run(engine, **{column: value})
    finally:
        engine.dispose()

    assert_refused_and_intact(database_path, config, f"runs.{column} differs")


@pytest.mark.parametrize("column", ("input_tokens", "output_tokens", "total_tokens"))
def test_the_downgrade_refuses_populated_attempt_token_accounting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, column: str
) -> None:
    """The accounting an earlier draft omitted, and the reason it is listed separately.

    A multi-turn Attempt's aggregate usage has no pre-0007 representation at all, so dropping the
    columns would silently lose it rather than merely relocate it.
    """
    database_path = tmp_path / f"d1-refuse-{column}.db"
    config, engine = migrate_to_head(database_path, monkeypatch)
    try:
        seed_run(engine)
        seed_job(engine)
        seed_attempt(engine)
        with engine.begin() as connection:
            connection.execute(text(f"UPDATE job_attempts SET {column}=42 WHERE id=1"))
    finally:
        engine.dispose()

    assert_refused_and_intact(database_path, config, "job_attempts holds Stage D token accounting")


def test_a_refused_downgrade_rewrites_nothing_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Several conditions at once: the refusal names them all, and the data is bit-identical."""
    database_path = tmp_path / "d1-refuse-many.db"
    config, engine = migrate_to_head(database_path, monkeypatch)
    try:
        seed_parents(engine)
        insert_row(engine, "mcp_connections", connection_row())
        insert_row(engine, "tool_definitions", definition_row())
        insert_row(engine, "agent_tool_grants", grant_row())
        insert_row(engine, "tool_invocations", invocation_row())
        insert_row(
            engine,
            "run_events",
            {
                "run_id": 1,
                "job_id": 1,
                "sequence": 1,
                "event_type": "tool.started",
                "code": "tool_started",
                "message": "started",
                "created_at": NOW,
            },
        )
        before = {
            table: scalar(engine, f"SELECT count(*) FROM {table}")
            for table in (*STAGE_D_TABLES, "run_events")
        }
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError) as failure:
        command.downgrade(config, C6_REVISION)
    message = str(failure.value)
    for expected in (
        "mcp_connections holds connection definitions",
        "tool_definitions holds discovered definitions",
        "agent_tool_grants holds capability grants",
        "tool_invocations holds tool-call audit rows",
        "run_events holds Stage D tool events",
    ):
        assert expected in message, expected

    engine = create_sqlite_engine(database_path)
    try:
        assert {
            table: scalar(engine, f"SELECT count(*) FROM {table}")
            for table in (*STAGE_D_TABLES, "run_events")
        } == before
        assert (
            scalar(engine, "SELECT event_type FROM run_events WHERE sequence=1") == "tool.started"
        )
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# Downgrade success, and the vocabulary it restores
# ---------------------------------------------------------------------------------------


def test_a_compatible_downgrade_succeeds_and_restores_the_0006_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0006 -> 0007 -> 0006 -> 0007 on a database holding only pre-D state."""
    database_path = tmp_path / "d1-clean-round-trip.db"
    config, engine = migrate_to_head(database_path, monkeypatch)
    try:
        seed_run(engine)
    finally:
        engine.dispose()

    command.downgrade(config, C6_REVISION)
    engine = create_sqlite_engine(database_path)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        for table in STAGE_D_TABLES:
            assert table not in tables, table
        assert "tool_grant_cutoff_id" not in {c["name"] for c in inspector.get_columns("runs")}
        assert "input_tokens" not in {c["name"] for c in inspector.get_columns("job_attempts")}
        assert "tool_invocation_id" not in {c["name"] for c in inspector.get_columns("run_events")}
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                C6_REVISION
            )
            assert connection.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
            # The pre-D vocabulary is restored: a tool event is no longer representable.
            assert connection.scalar(text("SELECT count(*) FROM runs")) == 1
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_sqlite_engine(database_path)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == D1_REVISION
            assert connection.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
            assert connection.scalar(text("SELECT count(*) FROM runs")) == 1
            assert connection.scalar(text("SELECT max_tool_calls FROM runs WHERE id=1")) == 0
    finally:
        engine.dispose()


def test_the_downgrade_removes_the_tool_event_vocabulary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At 0006 a tool event type must be rejected again, and a Stage C type must still work."""
    database_path = tmp_path / "d1-event-vocabulary.db"
    config, engine = migrate_to_head(database_path, monkeypatch)
    try:
        seed_parents(engine)
    finally:
        engine.dispose()

    command.downgrade(config, C6_REVISION)
    engine = create_sqlite_engine(database_path)
    try:
        insert_row(
            engine,
            "run_events",
            {
                "run_id": 1,
                "job_id": 1,
                "sequence": 1,
                "event_type": "run.queued",
                "created_at": NOW,
            },
        )
        with pytest.raises(Exception, match="ck_run_events_event_type_value"):
            insert_row(
                engine,
                "run_events",
                {
                    "run_id": 1,
                    "job_id": 1,
                    "sequence": 2,
                    "event_type": "tool.requested",
                    "created_at": NOW,
                },
            )
    finally:
        engine.dispose()
