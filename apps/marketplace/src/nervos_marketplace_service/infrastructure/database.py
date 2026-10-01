"""Bounded hosted PostgreSQL engine construction."""

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from nervos_marketplace_service.config import MarketplaceSettings

SCHEMA_HEAD = "mp0001_catalog_foundation"


def create_hosted_engine(settings: MarketplaceSettings) -> Engine:
    return create_engine(
        settings.database_dsn.get_secret_value(),
        hide_parameters=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_connect_timeout_seconds,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": settings.db_connect_timeout_seconds,
            "options": f"-c statement_timeout={settings.db_statement_timeout_ms} "
            f"-c transaction_timeout={settings.db_transaction_timeout_ms} "
            "-c timezone=UTC -c default_transaction_read_only=on",
        },
    )
