"""Alembic environment using the application's database configuration."""

from __future__ import annotations

from collections import Counter
from logging.config import fileConfig

from alembic import context
from alembic.operations import ops
from alembic.runtime.migration import MigrationContext
from alembic.util import CommandError
from nervos_api.config import Settings
from nervos_core.infrastructure.database import Base, build_sqlite_url, create_sqlite_engine
from nervos_core.infrastructure.database import models as database_models
from sqlalchemy import UniqueConstraint, inspect
from sqlalchemy.engine import Connection

_ = database_models
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def compare_colliding_unique_names(
    migration_context: MigrationContext, revision: object, directives: list[ops.MigrationScript]
) -> None:
    """Compare SQLite's legacy duplicate constraint names by their complete signatures.

    G3 has two distinct UNIQUE constraints sharing a generated name. Alembic's
    name-keyed comparison arbitrarily pairs them. Validate every UNIQUE signature
    on affected tables before discarding only the resulting spurious operations.
    This changes no schema, runtime metadata, or historical migration.
    """
    del revision
    connection = migration_context.connection
    if connection is None or connection.dialect.name != "sqlite":
        return
    inspector = inspect(connection)
    colliding: dict[str, set[str]] = {}
    for table in target_metadata.tables.values():
        if not inspector.has_table(table.name, schema=table.schema):
            continue
        expected = Counter(
            (str(constraint.name), tuple(sorted(column.name for column in constraint.columns)))
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        )
        actual = Counter(
            (str(constraint["name"]), tuple(sorted(constraint["column_names"])))
            for constraint in inspector.get_unique_constraints(table.name, schema=table.schema)
        )
        names = {
            name
            for signatures in (expected, actual)
            for name, count in Counter(name for name, _ in signatures.elements()).items()
            if count > 1
        }
        if not names:
            continue
        if expected != actual:
            raise CommandError(f"Unique constraint signature drift detected on {table.name}")
        colliding[table.name] = names
    for directive in directives:
        for changes in (*directive.upgrade_ops_list, *directive.downgrade_ops_list):
            for operation in changes.ops:
                if isinstance(operation, ops.ModifyTableOps):
                    names = colliding.get(operation.table_name, set())
                    operation.ops = [
                        item
                        for item in operation.ops
                        if not (
                            (
                                isinstance(item, ops.CreateUniqueConstraintOp)
                                or (
                                    isinstance(item, ops.DropConstraintOp)
                                    and item.constraint_type == "unique"
                                )
                            )
                            and item.constraint_name in names
                        )
                    ]


def run_migrations_offline() -> None:
    """Render migrations without opening a database connection."""
    settings = Settings()
    url = build_sqlite_url(settings.database_path)
    context.configure(
        url=url.render_as_string(hide_password=False),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations(connection: Connection) -> None:
    """Run migrations on an application-configured connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        process_revision_directives=compare_colliding_unique_names,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations with the same engine configuration as the application."""
    settings = Settings()
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_sqlite_engine(settings.database_path)
    try:
        with engine.connect() as connection:
            run_migrations(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
