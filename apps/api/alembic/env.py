"""Alembic environment using the application's database configuration."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from nervos_api.config import Settings
from nervos_core.infrastructure.database import Base, build_sqlite_url, create_sqlite_engine
from nervos_core.infrastructure.database import models as database_models
from sqlalchemy.engine import Connection

_ = database_models
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


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
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
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
