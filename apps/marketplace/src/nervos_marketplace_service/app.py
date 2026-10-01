"""Hosted composition root. Startup never migrates, seeds or executes packages."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy import text
from sqlalchemy.engine import Engine

from nervos_marketplace_service.api.errors import expected_error, unexpected_error
from nervos_marketplace_service.api.middleware import PublicReadMiddleware
from nervos_marketplace_service.api.router import router
from nervos_marketplace_service.application.artifact_reads import ArtifactReadService
from nervos_marketplace_service.application.catalog_queries import MarketplaceCatalogQueryService
from nervos_marketplace_service.application.ports import (
    ArtifactStore,
    CatalogRepository,
    DependencyReadiness,
)
from nervos_marketplace_service.config import MarketplaceSettings
from nervos_marketplace_service.domain.errors import MarketplaceError
from nervos_marketplace_service.infrastructure.catalog_repository import PostgresCatalogRepository
from nervos_marketplace_service.infrastructure.database import SCHEMA_HEAD, create_hosted_engine
from nervos_marketplace_service.infrastructure.s3_artifact_store import S3ArtifactStore


class HostedReadiness:
    def __init__(self, engine: Engine, store: S3ArtifactStore) -> None:
        self.engine = engine
        self.store = store

    def ready(self) -> bool:
        try:
            with self.engine.connect() as connection:
                revision = connection.execute(
                    text("SELECT version_num FROM marketplace_alembic_version")
                ).scalar_one()
                major = (
                    int(connection.execute(text("SHOW server_version_num")).scalar_one()) // 10000
                )
            return revision == SCHEMA_HEAD and major == 18 and self.store.ready()
        except Exception:
            return False


def create_test_app(
    repository: CatalogRepository, store: ArtifactStore, readiness: DependencyReadiness
) -> FastAPI:
    """Dependency composition usable by isolated tests; no production fixture/seed path."""
    catalog = MarketplaceCatalogQueryService(repository)
    app = FastAPI(title="NervOS Marketplace", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.catalog = catalog
    app.state.artifact_reads = ArtifactReadService(catalog, store)
    app.state.readiness = readiness
    app.add_middleware(PublicReadMiddleware)
    app.add_exception_handler(MarketplaceError, expected_error)
    app.add_exception_handler(RequestValidationError, expected_error)
    app.add_exception_handler(Exception, unexpected_error)
    app.include_router(router)
    return app


def create_app() -> FastAPI:
    engine: Engine | None = None
    try:
        settings = MarketplaceSettings()  # pyright: ignore[reportCallIssue]  # process environment
        engine = create_hosted_engine(settings)
        store = S3ArtifactStore(settings)
    except Exception:
        if engine is not None:
            engine.dispose()
        raise RuntimeError("Hosted Marketplace configuration/startup failed") from None
    logging.getLogger("nervos.marketplace").setLevel(settings.log_level)
    for name in ("boto3", "botocore", "urllib3", "sqlalchemy.engine"):
        logging.getLogger(name).setLevel(logging.WARNING)
    app = create_test_app(PostgresCatalogRepository(engine), store, HostedReadiness(engine, store))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            store.close()
            engine.dispose()

    app.router.lifespan_context = lifespan
    return app
