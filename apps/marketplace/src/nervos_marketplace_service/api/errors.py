"""Allowlisted public errors with no dependency details."""

from fastapi import Request
from fastapi.responses import JSONResponse

from nervos_marketplace_service.domain.errors import MarketplaceError

MESSAGES = {
    "invalid_request": "Invalid request.",
    "not_found": "Resource not found.",
    "release_yanked": "This release is yanked; exact acknowledgement is required.",
    "release_revoked": "This release is revoked and cannot be distributed.",
    "range_not_supported": "Partial downloads are not supported.",
    "rate_limited": "Artifact capacity is temporarily exhausted.",
    "artifact_unavailable": "The artifact is temporarily unavailable.",
    "service_unavailable": "The service is temporarily unavailable.",
    "internal_error": "An internal error occurred.",
}


def error_response(request: Request, error: MarketplaceError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status,
        content={
            "error": {
                "code": error.code,
                "message": MESSAGES.get(error.code, MESSAGES["internal_error"]),
                "request_id": request.state.request_id,
            }
        },
        headers={
            "Cache-Control": "no-store",
            "X-Request-ID": request.state.request_id,
            "X-Content-Type-Options": "nosniff",
            **({"Retry-After": "5"} if error.status == 429 else {}),
        },
    )


async def expected_error(request: Request, error: Exception) -> JSONResponse:
    return error_response(
        request,
        error if isinstance(error, MarketplaceError) else MarketplaceError("invalid_request"),
    )


async def unexpected_error(request: Request, error: Exception) -> JSONResponse:
    del error
    return error_response(request, MarketplaceError("internal_error", 500))
