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
from nervos_core.application.conversations import ConversationService
from nervos_core.application.mcp_connection_service import McpConnectionService
from nervos_core.application.run_cancellation import RunCancellationService
from nervos_core.application.triggers import TriggerManagementService
from nervos_core.application.webhooks import (
    WebhookDeliveryService,
    WebhookProvisioningService,
    WebhookSecretService,
)
from nervos_core.infrastructure.database import create_session_factory, create_sqlite_engine
from nervos_core.infrastructure.database.agents import SqlAlchemyAgentPersistence
from nervos_core.infrastructure.database.authentication import SqlAlchemyAuthenticationPersistence
from nervos_core.infrastructure.database.conversations import SqlAlchemyConversationPersistence
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobPersistence,
    SqlAlchemyRunCancellationPersistence,
)
from nervos_core.infrastructure.database.mcp_connections import (
    SqlAlchemyMcpConnectionPersistence,
)
from nervos_core.infrastructure.database.triggers import SqlAlchemyTriggerPersistence
from nervos_core.infrastructure.scheduling import create_schedule_evaluator
from nervos_core.infrastructure.security import Argon2PasswordHasher, SecureSessionTokens
from nervos_core.infrastructure.webhooks import (
    create_webhook_credential_factory,
    create_webhook_secret_verifier,
)
from nervos_mcp.adapters import OperatorFacts, build_discovery
from nervos_mcp.operator_config import load_operator_config
from nervos_mcp.policy.egress import StrictEgressPolicy
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
from nervos_api.hooks.router import router as hooks_router


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
    # The control plane's MCP surface. It is bound here -- in the composition root -- so every route
    # and every application module stays free of the SDK. An empty operator configuration refuses
    # every origin, server key and alias, which is the fail-closed default for an unconfigured host.
    operator_config = load_operator_config(
        allowed_origins=resolved_settings.mcp_allowed_origins,
        stdio_servers_json=resolved_settings.mcp_stdio_servers,
        credential_aliases_json=resolved_settings.mcp_credential_aliases,
    )
    egress_policy = StrictEgressPolicy(operator_config.allowed_origins)
    mcp_connection_service = McpConnectionService(
        SqlAlchemyMcpConnectionPersistence(engine),
        build_discovery(operator_config, egress_policy),
        OperatorFacts(operator_config, egress_policy),
        utc_now,
    )
    # Cancellation composes only the narrow control-plane store: this process gains the ability
    # to revoke authority over an owned Run, and no ability to claim, start, heartbeat,
    # terminalize, or reconcile execution.
    run_cancellation_service = RunCancellationService(
        SqlAlchemyRunCancellationPersistence(engine),
        agent_service,
        utc_now,
    )
    # The webhook ingress composes the trigger store and the credential verifier, and nothing else.
    # It is bound here -- in the composition root -- so the ingress module never names the concrete
    # persistence or the security primitives, and this process gains the ability to turn one
    # authenticated delivery into one ordinary Run, with no ability to execute it.
    webhook_ingress_service = WebhookDeliveryService(
        SqlAlchemyTriggerPersistence(
            engine,
            max_pending=resolved_settings.max_pending_jobs,
            max_pending_per_agent=resolved_settings.max_pending_jobs_per_agent,
            max_pending_per_provider=resolved_settings.max_pending_jobs_per_provider,
        ),
        create_builtin_definition_registry(),
        create_webhook_secret_verifier(),
    )
    # Trigger management: owner-scoped configuration, schedule state, and webhook credentials.
    # The E2 schedule evaluator is the *only* place schedule arithmetic happens.
    trigger_persistence = SqlAlchemyTriggerPersistence(
        engine,
        max_pending=resolved_settings.max_pending_jobs,
        max_pending_per_agent=resolved_settings.max_pending_jobs_per_agent,
        max_pending_per_provider=resolved_settings.max_pending_jobs_per_provider,
    )
    schedule_evaluator = create_schedule_evaluator()
    factory = create_webhook_credential_factory()
    webhook_provisioning = WebhookProvisioningService(trigger_persistence, factory)
    webhook_secrets = WebhookSecretService(trigger_persistence, factory)
    trigger_management_service = TriggerManagementService(
        trigger_persistence,
        webhook_provisioning,
        webhook_secrets,
        schedule_evaluator,
        utc_now,
    )
    conversation_persistence = SqlAlchemyConversationPersistence(
        engine,
        max_pending=resolved_settings.max_pending_jobs,
        max_pending_per_agent=resolved_settings.max_pending_jobs_per_agent,
        max_pending_per_provider=resolved_settings.max_pending_jobs_per_provider,
    )
    conversation_service = ConversationService(
        conversation_persistence,
        agent_service,
        clock=utc_now,
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
    app.state.mcp_connection_service = mcp_connection_service
    app.state.webhook_ingress_service = webhook_ingress_service
    app.state.trigger_management_service = trigger_management_service
    app.state.conversation_service = conversation_service
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
    # A second, sibling include. The management router above is untouched: the ingress is a
    # different contract with a different credential, so it is composed beside `/api/v1` rather
    # than added to it, and the `Origin`/CSRF boundary that guards `/api/v1` never sees this path.
    app.include_router(hooks_router)
    return app
