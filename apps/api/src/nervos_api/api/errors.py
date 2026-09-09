"""Safe public error handling for authentication endpoints."""

from __future__ import annotations

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from nervos_core.application.authentication import (
    AuthenticationRequired,
    InvalidCredentials,
    InvalidPassword,
    InvalidUsername,
    PasswordWorkLimit,
    PersistenceUnavailable,
    SetupComplete,
)

from nervos_api.api.schemas import ErrorDetail, ErrorResponse


class InvalidOrigin(Exception):
    """Raised when an unsafe request lacks the exact configured Origin."""


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
