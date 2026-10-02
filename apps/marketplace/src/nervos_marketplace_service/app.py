"""Hosted composition root. Startup never migrates, seeds or executes packages."""

import base64
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy import text
from sqlalchemy.engine import Engine

from nervos_marketplace_service.api.errors import expected_error, unexpected_error
from nervos_marketplace_service.api.middleware import PublicReadMiddleware
from nervos_marketplace_service.api.publisher_router import router as publisher_router
from nervos_marketplace_service.api.router import router
from nervos_marketplace_service.application.artifact_reads import ArtifactReadService
from nervos_marketplace_service.application.authentication import Authentication
from nervos_marketplace_service.application.authorization import Authorization
from nervos_marketplace_service.application.catalog_queries import MarketplaceCatalogQueryService
from nervos_marketplace_service.application.ports import (
    ArtifactStore,
    CatalogRepository,
    DependencyReadiness,
)
from nervos_marketplace_service.application.publication import Publication
from nervos_marketplace_service.application.publisher_management import PublisherManagement
from nervos_marketplace_service.config import MarketplaceSettings
from nervos_marketplace_service.domain.errors import MarketplaceError
from nervos_marketplace_service.domain.identity import Assurance
from nervos_marketplace_service.infrastructure.catalog_repository import PostgresCatalogRepository
from nervos_marketplace_service.infrastructure.database import (
    SCHEMA_HEAD,
    create_hosted_engine,
    create_writer_engine,
)
from nervos_marketplace_service.infrastructure.oidc import OIDCClient
from nervos_marketplace_service.infrastructure.publication_storage import S3PublicationStorage
from nervos_marketplace_service.infrastructure.s3_artifact_store import S3ArtifactStore
from nervos_marketplace_service.infrastructure.static_verifier import StaticVerifier
from nervos_marketplace_service.infrastructure.unit_of_work import PostgresUnitOfWork


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

    oidc: OIDCClient | None = None
    writer: Engine | None = None
    publication_store: S3PublicationStorage | None = None
    if settings.publisher_enabled:
        oidc = OIDCClient(settings)
        writer = create_writer_engine(settings)
        uow = PostgresUnitOfWork(writer)
        key = (
            base64.b64decode(
                settings.auth_transaction_encryption_key.get_secret_value(), validate=True
            )
            if settings.auth_transaction_encryption_key
            else b""
        )
        authentication = Authentication(
            uow,
            oidc,
            key,
            settings.session_absolute_seconds,
            settings.session_idle_seconds,
            settings.cli_token_seconds,
        )
        assurance = Assurance(
            settings.accepted_acr_values,
            settings.required_amr_values,
            settings.recent_auth_max_age_seconds,
        )
        app.state.publisher_authentication = authentication
        app.state.publisher_management = PublisherManagement(
            uow,
            Authorization(assurance),
            settings.oidc_client_id or "nervos-marketplace",
            settings.challenges_per_minute,
            settings.projects_per_publisher,
        )
        publication_store = S3PublicationStorage(settings)
        app.state.publication = Publication(
            uow,
            Authorization(assurance),
            publication_store,
            settings.artifact_temp_directory,
            timeout=settings.upload_timeout_seconds,
            idle=settings.upload_idle_seconds,
            account_uploads=settings.active_uploads_per_account,
            publisher_uploads=settings.active_uploads_per_publisher,
            daily_bytes=settings.daily_upload_bytes,
        )
        app.state.static_verifier = StaticVerifier(
            settings.verifier_memory_bytes,
            settings.verifier_cpu_seconds,
            settings.verifier_wall_seconds,
        )
        app.include_router(publisher_router)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            store.close()
            if oidc is not None:
                await oidc.close()
            if writer is not None:
                writer.dispose()
            if publication_store is not None:
                publication_store.close()
            engine.dispose()

    app.router.lifespan_context = lifespan
    return app
