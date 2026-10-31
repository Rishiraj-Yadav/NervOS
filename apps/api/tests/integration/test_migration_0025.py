# 0025 — process-kind package install migration tests (ADR 0046).

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, IntegrityError
from test_migrations import (  # pyright: ignore[reportMissingImports]
    alembic_config,
    create_sqlite_engine,
    migrate_database,
)

LEGACY_DIGEST = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def _insert_python_row(connection: Connection) -> None:
    connection.execute(
        text(
            "INSERT INTO installed_package_versions ("
            "package_id, package_version, status, content_digest, archive_digest, "
            "signer_public_key, signer_fingerprint, manifest_bytes, config_schema_bytes, "
            "dependency_lock_bytes, agent_wheel_name, agent_wheel_version, agent_wheel_sha256, "
            "agent_wheel_size, entrypoint_module, entrypoint_object, approved_at, created_at, "
            "updated_at) VALUES ("
            "'com.acme.legacy', '1.0.0', 'active', "
            f"'{LEGACY_DIGEST}', '{LEGACY_DIGEST}', "
            "zeroblob(32), "
            f"'{LEGACY_DIGEST}', "
            "x'00', x'00', x'00', 'agent.whl', '1.0.0', "
            f"'{LEGACY_DIGEST}', "
            "1, 'acme.agent', 'Agent', '2026-01-01 00:00:00', "
            "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        )
    )


def _insert_process_row(connection: Connection) -> None:
    connection.execute(
        text(
            "INSERT INTO installed_package_versions ("
            "package_id, package_version, status, content_digest, archive_digest, "
            "signer_public_key, signer_fingerprint, manifest_bytes, config_schema_bytes, "
            "dependency_lock_bytes, agent_wheel_name, agent_wheel_version, "
            "agent_wheel_sha256, agent_wheel_size, runtime_kind, approved_at, "
            "created_at, updated_at) VALUES ("
            "'com.acme.process', '1.0.0', 'active', "
            f"'{LEGACY_DIGEST}', '{LEGACY_DIGEST}', "
            "zeroblob(32), "
            f"'{LEGACY_DIGEST}', "
            "x'00', x'00', x'00', 'agent.whl', '1.0.0', "
            f"'{LEGACY_DIGEST}', "
            "1, 'process', '2026-01-01 00:00:00', "
            "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        )
    )


def test_0025_upgrade_keeps_python_rows_valid_and_adds_the_pairing_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing python rows survive the upgrade with their entrypoints intact."""
    database_path = tmp_path / "0025-upgrade.db"
    config = alembic_config(database_path, monkeypatch)
    command.upgrade(config, "0024_durable_workflows")
    engine = create_sqlite_engine(database_path)
    try:
        with engine.begin() as connection:
            _insert_python_row(connection)
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_sqlite_engine(database_path)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT runtime_kind, entrypoint_module, entrypoint_object "
                    "FROM installed_package_versions"
                )
            ).one()
        assert row == ("python", "acme.agent", "Agent")
        checks = {
            item["name"]
            for item in inspect(engine).get_check_constraints("installed_package_versions")
        }
        assert "ck_installed_package_versions_runtime_kind_value" in checks
        assert "ck_installed_package_versions_runtime_entrypoint_pairing" in checks
    finally:
        engine.dispose()


def test_0025_checks_accept_valid_rows_and_reject_invalid_mixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pairing CHECK admits both kinds and refuses every invalid mix."""
    config, engine = migrate_database(tmp_path / "0025-checks.db", monkeypatch)
    del config
    try:
        with engine.begin() as connection:
            _insert_python_row(connection)
            _insert_process_row(connection)
        for statement in (
            # process with an entrypoint
            "UPDATE installed_package_versions SET entrypoint_module = 'x'"
            " WHERE package_id = 'com.acme.process'",
            # python without an entrypoint
            "UPDATE installed_package_versions SET entrypoint_module = NULL"
            " WHERE package_id = 'com.acme.legacy'",
            # unknown kind
            "UPDATE installed_package_versions SET runtime_kind = 'wasm'"
            " WHERE package_id = 'com.acme.legacy'",
        ):
            with (
                pytest.raises((IntegrityError, DBAPIError)),
                engine.begin() as connection,
            ):
                connection.execute(text(statement))
    finally:
        engine.dispose()


def test_0025_downgrade_refuses_process_rows_and_restores_the_old_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Downgrade refuses while process rows exist; otherwise 0024 is restored exactly."""
    database_path = tmp_path / "0025-downgrade.db"
    config = alembic_config(database_path, monkeypatch)
    command.upgrade(config, "head")
    engine = create_sqlite_engine(database_path)
    try:
        with engine.begin() as connection:
            _insert_python_row(connection)
            _insert_process_row(connection)
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="refuses to run"):
        command.downgrade(config, "0024_durable_workflows")

    engine = create_sqlite_engine(database_path)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM installed_package_versions WHERE package_id = 'com.acme.process'")
            )
    finally:
        engine.dispose()

    command.downgrade(config, "0024_durable_workflows")
    engine = create_sqlite_engine(database_path)
    try:
        columns = {
            col["name"]: col for col in inspect(engine).get_columns("installed_package_versions")
        }
        assert "runtime_kind" not in columns
        assert columns["entrypoint_module"]["nullable"] is False
        assert columns["entrypoint_object"]["nullable"] is False
        checks = {
            item["name"]
            for item in inspect(engine).get_check_constraints("installed_package_versions")
        }
        assert "ck_installed_package_versions_runtime_kind_value" not in checks
        assert "ck_installed_package_versions_runtime_entrypoint_pairing" not in checks
        # The python row survived with its entrypoint intact.
        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT entrypoint_module, entrypoint_object FROM installed_package_versions")
            ).one()
        assert row == ("acme.agent", "Agent")
    finally:
        engine.dispose()
