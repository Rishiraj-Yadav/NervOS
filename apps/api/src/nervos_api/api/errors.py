"""Safe public error handling for the NervOS API."""

from __future__ import annotations

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from nervos_core.application.agent_definitions import UnknownAgentDefinition
from nervos_core.application.agents import (
    AgentInstanceNotFound,
    AgentInstanceUnavailable,
    RunNotFound,
)
from nervos_core.application.authentication import (
    AuthenticationRequired,
    InvalidCredentials,
    InvalidPassword,
    InvalidUsername,
    PasswordWorkLimit,
    PersistenceUnavailable,
    SetupComplete,
)
from nervos_core.application.errors import (
    PersistenceUnavailable as ApplicationPersistenceUnavailable,
)
from nervos_core.application.errors import QueueCapacityExceeded
from nervos_core.application.model_providers import (
    ModelProviderUnavailable,
    UnknownModelProvider,
)
from nervos_core.application.run_cancellation import RunNotCancellable
from nervos_core.application.trusted_chat import UnknownAgentHandler
from nervos_core.domain.agents import InvalidAgentDefinitionId, InvalidAgentInstance
from nervos_core.domain.runs import InvalidRun

from nervos_api.api.schemas import ErrorDetail, ErrorResponse


class InvalidOrigin(Exception):
    """Raised when an unsafe request lacks the exact configured Origin."""


class UnsupportedB3AgentDefinition(Exception):
    """Raised when a structurally valid definition identity is outside this milestone's surface.

    The resolver still decides whether an exact pair exists. This is a narrower NervOS-owned
    surface restriction that keeps later-registered definitions from becoming creatable here
    before their own milestone authorizes it.
    """


_ERROR_MAP: dict[type[Exception], tuple[int, str, str]] = {
    InvalidUsername: (422, "invalid_username", "Username does not meet the required format."),
    InvalidPassword: (422, "invalid_password", "Password does not meet the required length."),
    SetupComplete: (409, "setup_complete", "Initial setup is already complete."),
    InvalidCredentials: (401, "invalid_credentials", "Invalid username or password."),
    AuthenticationRequired: (401, "authentication_required", "Authentication is required."),
    PersistenceUnavailable: (
        503,
        "service_unavailable",
        "Authentication is temporarily unavailable.",
    ),
    InvalidOrigin: (403, "invalid_origin", "Request origin is not allowed."),
    PasswordWorkLimit: (429, "too_many_attempts", "Too many authentication attempts."),
}

# Agent/Run domain failures. Dispatched by exact type through `agent_error_handler`, which is
# registered for every key here. Static NervOS-owned messages only: no provider text, body,
# header, credential, or stack trace is ever propagated.
AGENT_ERROR_MAP: dict[type[Exception], tuple[int, str, str]] = {
    InvalidAgentDefinitionId: (
        422,
        "invalid_agent_definition",
        "The agent definition identity is invalid.",
    ),
    UnsupportedB3AgentDefinition: (
        422,
        "unsupported_agent_definition",
        "This NervOS milestone only supports the trusted nervos.chat version 1 definition.",
    ),
    UnknownAgentDefinition: (
        422,
        "unknown_agent_definition",
        "The requested agent definition version is not available.",
    ),
    UnknownModelProvider: (422, "unknown_model_provider", "The model provider is not supported."),
    InvalidAgentInstance: (
        422,
        "invalid_agent_instance",
        "The agent instance configuration is invalid.",
    ),
    InvalidRun: (422, "invalid_input", "The submitted input is invalid."),
    AgentInstanceNotFound: (404, "agent_instance_not_found", "The agent instance was not found."),
    RunNotFound: (404, "run_not_found", "The run was not found."),
    RunNotCancellable: (
        409,
        "run_not_cancellable",
        "The run already reached a terminal state and cannot be cancelled.",
    ),
    AgentInstanceUnavailable: (
        409,
        "agent_instance_unavailable",
        "The agent instance is not eligible to create new runs.",
    ),
    ModelProviderUnavailable: (
        409,
        "model_provider_unavailable",
        "The model provider is not configured for this NervOS process.",
    ),
    UnknownAgentHandler: (
        503,
        "service_unavailable",
        "The trusted agent behavior is unavailable.",
    ),
    QueueCapacityExceeded: (
        429,
        "queue_capacity_exceeded",
        "NervOS cannot accept more runs until its pending queue drains.",
    ),
    ApplicationPersistenceUnavailable: (
        503,
        "service_unavailable",
        "NervOS storage is temporarily unavailable.",
    ),
}


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    """Build a stable non-cacheable public error response."""
    content = ErrorResponse(error=ErrorDetail(code=code, message=message)).model_dump()
    return JSONResponse(
        status_code=status_code,
        content=content,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


async def unexpected_error_handler(request: Request, error: Exception) -> JSONResponse:
    """Return a generic production-safe response for unexpected failures."""
    del request, error
    return error_response(500, "internal_server_error", "An unexpected error occurred.")


async def authentication_error_handler(request: Request, error: Exception) -> JSONResponse:
    """Map expected authentication failures without exposing internals."""
    del request
    status_code, code, message = _ERROR_MAP[type(error)]
    return error_response(status_code, code, message)


async def agent_error_handler(request: Request, error: Exception) -> JSONResponse:
    """Map expected Agent Instance and Run failures without exposing internals."""
    del request
    status_code, code, message = AGENT_ERROR_MAP[type(error)]
    return error_response(status_code, code, message)


async def validation_error_handler(
    request: Request,
    error: RequestValidationError,
) -> JSONResponse:
    """Return sanitized validation details without rejected input values."""
    del request
    allowed_fields = {"username", "password"}
    fields = sorted(
        {
            str(part)
            for item in error.errors()
            for part in item["loc"]
            if isinstance(part, str) and part in allowed_fields
        }
    )
    message = "Request validation failed."
    if fields:
        message = f"Request validation failed for: {', '.join(fields)}."
    return error_response(422, "validation_error", message)
