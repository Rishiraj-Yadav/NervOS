"""FastAPI application composition.

The control plane accepts work and never executes it. It composes no handler registry, no
executor, and no credential-bearing provider client, so "the API cannot run a model" is a
structural property rather than a convention.
"""

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
from nervos_core.application.run_cancellation import RunCancellationService
from nervos_core.infrastructure.database import create_session_factory, create_sqlite_engine
from nervos_core.infrastructure.database.agents import SqlAlchemyAgentPersistence
from nervos_core.infrastructure.database.authentication import SqlAlchemyAuthenticationPersistence
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobPersistence,
    SqlAlchemyRunCancellationPersistence,
)
from nervos_core.infrastructure.security import Argon2PasswordHasher, SecureSessionTokens
from nervos_models import compose_model_providers

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
    # Composed with no credential at all: this yields the known-provider set and constructs
    # zero clients, so nothing credential-bearing is reachable from ``app.state``.
    known_providers = compose_model_providers(None, None).catalog
    agent_service = AgentService(
        SqlAlchemyAgentPersistence(session_factory),
        create_builtin_definition_registry(),
        utc_now,
        known_providers,
        SqlAlchemyJobPersistence(
            engine,
            max_pending=resolved_settings.max_pending_jobs,
            max_pending_per_agent=resolved_settings.max_pending_jobs_per_agent,
            max_pending_per_provider=resolved_settings.max_pending_jobs_per_provider,
        ),
    )
    # Cancellation composes only the narrow control-plane store: this process gains the ability
    # to revoke authority over an owned Run, and no ability to claim, start, heartbeat,
    # terminalize, or reconcile execution.
    run_cancellation_service = RunCancellationService(
        SqlAlchemyRunCancellationPersistence(engine),
        agent_service,
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
    app.state.agent_service = agent_service
    # The route depends on the submission service; it is the same owner-scoped Agent service,
    # exposed under the name that describes what the cutover made it responsible for.
    app.state.run_submission_service = agent_service
    app.state.run_cancellation_service = run_cancellation_service
    app.state.model_provider_catalog = known_providers
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
