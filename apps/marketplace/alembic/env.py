"""Hosted migration process: deployment credentials, never local runtime metadata."""

import os

from alembic import context
from nervos_marketplace_service.infrastructure.models import HostedBase
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

dsn = os.environ.get("NERVOS_MARKETPLACE_DATABASE_DSN", "")
try:
    url = make_url(dsn)
    if url.drivername != "postgresql+psycopg":
        raise ValueError
except Exception:
    raise RuntimeError("A hosted PostgreSQL migration DSN is required") from None

if context.is_offline_mode():
    context.configure(
        url=url,
        target_metadata=HostedBase.metadata,
        version_table="marketplace_alembic_version",
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, hide_parameters=True)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=HostedBase.metadata,
                version_table="marketplace_alembic_version",
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()
