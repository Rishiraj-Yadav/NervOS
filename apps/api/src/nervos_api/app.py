"""FastAPI application composition."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from nervos_core.application.agent_definitions import create_builtin_definition_registry
from nervos_core.application.agents import AgentService
from nervos_core.application.authentication import (
    AuthenticationError,
    AuthenticationService,
)
from nervos_core.application.run_coordinator import (
    RunCoordinator,
    system_monotonic_nanoseconds,
)
from nervos_core.application.trusted_chat import create_builtin_handler_registry
from nervos_core.infrastructure.database import create_session_factory, create_sqlite_engine
from nervos_core.infrastructure.database.agents import SqlAlchemyAgentPersistence
from nervos_core.infrastructure.database.authentication import SqlAlchemyAuthenticationPersistence
from nervos_core.infrastructure.security import Argon2PasswordHasher, SecureSessionTokens
from nervos_models import close_model_providers, compose_model_providers

from nervos_api.api.dependencies import utc_now
from nervos_api.api.errors import (
    AGENT_ERROR_MAP,
    InvalidOrigin,
    agent_error_handler,
    authentication_error_handler,
    unexpected_error_handler,
    validation_error_handler,
)
from nervos_api.api.middleware import ApiSecurityHeadersMiddleware, AuthenticationBoundaryMiddleware
from nervos_api.api.router import api_router
from nervos_api.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Compose the API without connecting to the database or contacting any provider."""
    resolved_settings = settings or get_settings()
    engine = create_sqlite_engine(resolved_settings.database_path)
    session_factory = create_session_factory(engine)
    authentication_service = AuthenticationService(
        SqlAlchemyAuthenticationPersistence(session_factory),
        Argon2PasswordHasher(),
        SecureSessionTokens(),
        utc_now,
    )
    handlers = create_builtin_handler_registry()
    anthropic_secret = (
        resolved_settings.anthropic_api_key.get_secret_value()
        if resolved_settings.anthropic_api_key is not None
        else None
    )
    openai_secret = (
        resolved_settings.openai_api_key.get_secret_value()
        if resolved_settings.openai_api_key is not None
        else None
    )
    # Both credentials are now held only by the clients built below. Everything reachable
    # after composition - the middleware and every ``app.state`` consumer - gets this
    # credential-free copy instead, so no secret-bearing settings object is reachable from
    # the running application.
    sanitized_settings = resolved_settings.without_provider_credentials()
    model_providers = compose_model_providers(anthropic_secret, openai_secret)
    providers = model_providers.catalog
    agent_service = AgentService(
        SqlAlchemyAgentPersistence(session_factory),
        create_builtin_definition_registry(),
        utc_now,
        handlers,
        providers,
    )
    run_coordinator = RunCoordinator(
        agent_service, handlers, providers, system_monotonic_nanoseconds
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        del app
        yield
        await close_model_providers(model_providers)
        engine.dispose()

    app = FastAPI(title="NervOS API", lifespan=lifespan)
    app.add_middleware(AuthenticationBoundaryMiddleware, settings=sanitized_settings)
    app.add_middleware(ApiSecurityHeadersMiddleware)
    app.state.settings = sanitized_settings
    app.state.database_engine = engine
    app.state.session_factory = session_factory
    app.state.authentication_service = authentication_service
    app.state.agent_service = agent_service
    app.state.model_provider_catalog = providers
    app.state.run_coordinator = run_coordinator
    app.add_exception_handler(Exception, unexpected_error_handler)
    app.add_exception_handler(AuthenticationError, authentication_error_handler)
    app.add_exception_handler(InvalidOrigin, authentication_error_handler)
    for agent_error_type in AGENT_ERROR_MAP:
        app.add_exception_handler(agent_error_type, agent_error_handler)
    app.add_exception_handler(
        RequestValidationError,
        validation_error_handler,  # pyright: ignore[reportArgumentType]
    )
    app.include_router(api_router)
    return app
