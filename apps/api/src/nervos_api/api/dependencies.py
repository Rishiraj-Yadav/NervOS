"""FastAPI dependencies for authentication and request security."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, cast

from fastapi import Cookie, Depends, Request
from nervos_core.application.agents import AgentService
from nervos_core.application.authentication import (
    AuthenticationRequired,
    AuthenticationService,
    PublicUser,
)
from nervos_core.application.conversations import ConversationService
from nervos_core.application.mcp_connection_service import McpConnectionService
from nervos_core.application.model_providers import ModelProviderCatalog
from nervos_core.application.run_cancellation import RunCancellationService
from nervos_core.application.triggers import TriggerManagementService

from nervos_api.api.cookies import SESSION_COOKIE_NAME
from nervos_api.api.errors import InvalidOrigin
from nervos_api.config import Settings


def utc_now() -> datetime:
    """Return the current aware UTC instant."""
    return datetime.now(UTC)


def get_settings(request: Request) -> Settings:
    """Return the settings composed into this FastAPI application."""
    return cast(Settings, request.app.state.settings)


def get_authentication_service(request: Request) -> AuthenticationService:
    """Return the concrete authentication service composed into the app."""
    return cast(AuthenticationService, request.app.state.authentication_service)


def get_agent_service(request: Request) -> AgentService:
    """Return the application-owned Agent Instance and Run service."""
    return cast(AgentService, request.app.state.agent_service)


def get_run_submission_service(request: Request) -> AgentService:
    """Return the durable Run submission service; routes never build one."""
    return cast(AgentService, request.app.state.run_submission_service)


def get_run_cancellation_service(request: Request) -> RunCancellationService:
    """Return the owner-authorized Run cancellation service; routes never build one."""
    return cast(RunCancellationService, request.app.state.run_cancellation_service)


def get_mcp_connection_service(request: Request) -> McpConnectionService:
    """Return the owner-scoped MCP connection service; routes never build one."""
    return cast(McpConnectionService, request.app.state.mcp_connection_service)


def get_trigger_management_service(request: Request) -> TriggerManagementService:
    """Return the owner-scoped trigger management service; routes never build one."""
    return cast(TriggerManagementService, request.app.state.trigger_management_service)


def get_model_provider_catalog(request: Request) -> ModelProviderCatalog:
    """Return the composed provider catalog used only for safe local preflight."""
    return cast(ModelProviderCatalog, request.app.state.model_provider_catalog)


def get_conversation_service(request: Request) -> ConversationService:
    """Return the owner-scoped conversation service; routes never build one."""
    return cast(ConversationService, request.app.state.conversation_service)


def require_configured_origin(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """Reject every absent or non-exact Origin before sensitive work."""
    if request.headers.get("origin") != settings.app_origin:
        raise InvalidOrigin


def get_current_user(
    service: Annotated[AuthenticationService, Depends(get_authentication_service)],
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
) -> PublicUser:
    """Resolve authenticated identity solely from the opaque session cookie."""
    if session_token is None:
        raise AuthenticationRequired
    return service.authenticate(session_token)


SettingsDependency = Annotated[Settings, Depends(get_settings)]
AuthenticationServiceDependency = Annotated[
    AuthenticationService,
    Depends(get_authentication_service),
]
AgentServiceDependency = Annotated[AgentService, Depends(get_agent_service)]
RunSubmissionDependency = Annotated[AgentService, Depends(get_run_submission_service)]
RunCancellationDependency = Annotated[
    RunCancellationService,
    Depends(get_run_cancellation_service),
]
ModelProviderCatalogDependency = Annotated[
    ModelProviderCatalog,
    Depends(get_model_provider_catalog),
]
McpConnectionServiceDependency = Annotated[
    McpConnectionService,
    Depends(get_mcp_connection_service),
]
TriggerManagementDependency = Annotated[
    TriggerManagementService,
    Depends(get_trigger_management_service),
]
ConversationServiceDependency = Annotated[
    ConversationService,
    Depends(get_conversation_service),
]
OriginDependency = Annotated[None, Depends(require_configured_origin)]
CurrentUserDependency = Annotated[PublicUser, Depends(get_current_user)]
