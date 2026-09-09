"""FastAPI application composition."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from nervos_core.application.authentication import (
    AuthenticationError,
    AuthenticationService,
)
from nervos_core.infrastructure.database import create_session_factory, create_sqlite_engine
from nervos_core.infrastructure.database.authentication import SqlAlchemyAuthenticationPersistence
from nervos_core.infrastructure.security import Argon2PasswordHasher, SecureSessionTokens

from nervos_api.api.dependencies import utc_now
from nervos_api.api.errors import (
    InvalidOrigin,
    authentication_error_handler,
    unexpected_error_handler,
    validation_error_handler,
)
from nervos_api.api.middleware import ApiSecurityHeadersMiddleware, AuthenticationBoundaryMiddleware
from nervos_api.api.router import api_router
from nervos_api.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Compose the API without connecting to or migrating the database."""
    resolved_settings = settings or get_settings()
    engine = create_sqlite_engine(resolved_settings.database_path)
    session_factory = create_session_factory(engine)
    authentication_service = AuthenticationService(
        SqlAlchemyAuthenticationPersistence(session_factory),
        Argon2PasswordHasher(),
        SecureSessionTokens(),
        utc_now,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        del app
        yield
        engine.dispose()

    app = FastAPI(title="NervOS API", lifespan=lifespan)
    app.add_middleware(AuthenticationBoundaryMiddleware, settings=resolved_settings)
    app.add_middleware(ApiSecurityHeadersMiddleware)
    app.state.settings = resolved_settings
    app.state.database_engine = engine
    app.state.session_factory = session_factory
    app.state.authentication_service = authentication_service
    app.add_exception_handler(Exception, unexpected_error_handler)
    app.add_exception_handler(AuthenticationError, authentication_error_handler)
    app.add_exception_handler(InvalidOrigin, authentication_error_handler)
    app.add_exception_handler(
        RequestValidationError,
        validation_error_handler,  # pyright: ignore[reportArgumentType]
    )
    app.include_router(api_router)
    return app
