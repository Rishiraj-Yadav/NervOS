"""The webhook ingress's public response contract.

Deliberately **smaller and less descriptive** than the management API's error envelope. The ingress
answers a machine rather than a browser session, so every refusal is a static code with a static
message and nothing else: no rejected value is ever reflected back, no internal reason is named, and
no durable state can be inferred from wording.

Two properties are load-bearing rather than cosmetic:

* **Every authentication failure is the same response**, produced by the same constant, so an
  unknown locator, a malformed locator, a missing secret, a wrong scheme and a wrong secret cannot
  be told apart by their answers.
* **Every response carries `Cache-Control: no-store`.** The management boundary's header middleware
  is scoped to `/api/v1`, so this namespace sets its own headers instead of reaching into a
  boundary it must not widen.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse
from nervos_core.application.webhooks import WebhookDeliveryKind, WebhookDeliveryResult

from nervos_api.hooks.schemas import WebhookAcceptedResponse, WebhookErrorResponse

#: Advisory only: a well-behaved sender should apply its own backoff. The value exists so a
#: retryable refusal is actionable rather than a bare status.
RETRY_AFTER_SECONDS = "1"

#: One static triple per refusal kind. Static messages only -- never a value the caller supplied,
#: never a parser's message, never an internal reason, never a database detail.
_REFUSALS: dict[WebhookDeliveryKind, tuple[int, str, str]] = {
    # The one answer every authentication failure gets, byte for byte.
    WebhookDeliveryKind.UNAUTHENTICATED: (
        404,
        "not_found",
        "The requested webhook is not available.",
    ),
    WebhookDeliveryKind.DISABLED: (
        403,
        "webhook_unavailable",
        "This webhook is currently unavailable.",
    ),
    WebhookDeliveryKind.UNSUPPORTED_MEDIA_TYPE: (
        415,
        "unsupported_media_type",
        "Content-Type must be application/json.",
    ),
    WebhookDeliveryKind.MALFORMED_PAYLOAD: (
        400,
        "malformed_payload",
        "The request body is not a valid JSON object.",
    ),
    WebhookDeliveryKind.INVALID_IDEMPOTENCY_KEY: (
        400,
        "invalid_idempotency_key",
        "The Idempotency-Key header is not valid.",
    ),
    WebhookDeliveryKind.CONFLICT: (
        409,
        "idempotency_conflict",
        "This Idempotency-Key was already used with a different payload.",
    ),
    WebhookDeliveryKind.CAPACITY_EXCEEDED: (
        503,
        "service_unavailable",
        "The service is temporarily unavailable.",
    ),
    WebhookDeliveryKind.TEMPORARILY_UNAVAILABLE: (
        503,
        "service_unavailable",
        "The service is temporarily unavailable.",
    ),
}

#: The refusals a sender may safely retry: nothing was recorded, so the delivery was neither lost
#: nor consumed, and a retry bearing the same idempotency key is still the first use of that key.
_RETRYABLE = frozenset(
    {WebhookDeliveryKind.CAPACITY_EXCEEDED, WebhookDeliveryKind.TEMPORARILY_UNAVAILABLE}
)

#: The transport-level refusal the route raises before the service is ever consulted.
_BODY_TOO_LARGE = (413, "request_too_large", "Request body is too large.")

#: The last-resort refusal. It names nothing, because an unexpected failure has no safe name.
_INTERNAL = (500, "internal_error", "An unexpected error occurred.")


def _headers(retry_after: bool = False) -> dict[str, str]:
    headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }
    if retry_after:
        headers["Retry-After"] = RETRY_AFTER_SECONDS
    return headers


def _refusal(spec: tuple[int, str, str], *, retryable: bool = False) -> JSONResponse:
    status_code, code, message = spec
    return JSONResponse(
        status_code=status_code,
        content=WebhookErrorResponse(code=code, message=message).model_dump(),
        headers=_headers(retry_after=retryable),
    )


def not_found() -> JSONResponse:
    """The single authentication-failure answer, produced from one constant everywhere."""
    return _refusal(_REFUSALS[WebhookDeliveryKind.UNAUTHENTICATED])


def body_too_large() -> JSONResponse:
    """A transport bound was exceeded. Refused before parsing and before any durable work."""
    return _refusal(_BODY_TOO_LARGE)


def unsupported_media_type() -> JSONResponse:
    """A syntax-level refusal the route makes before the service is consulted."""
    return _refusal(_REFUSALS[WebhookDeliveryKind.UNSUPPORTED_MEDIA_TYPE])


def internal_error() -> JSONResponse:
    return _refusal(_INTERNAL)


def refused(result: WebhookDeliveryResult) -> JSONResponse:
    """Map a service refusal onto its static response."""
    spec = _REFUSALS.get(result.kind, _INTERNAL)
    return _refusal(spec, retryable=result.kind in _RETRYABLE)


def accepted(result: WebhookDeliveryResult) -> JSONResponse:
    """The accepted answer: an occurrence now represents this delivery.

    `202` is returned for a created Run, for a skip and for a duplicate alike, so the status carries
    exactly one meaning -- *this delivery is accounted for* -- and the `duplicate` flag carries the
    distinction. A sender therefore never has to branch on the status to learn whether its retry
    landed, and a skipped delivery is never mistaken for one worth retrying.

    The occurrence id is guaranteed non-null by the result's own invariant; the fallback exists so a
    broken invariant becomes a boring 500 rather than a response claiming a null occurrence.
    """
    occurrence_id = result.occurrence_id
    if occurrence_id is None:
        return internal_error()
    return JSONResponse(
        status_code=202,
        content=WebhookAcceptedResponse(
            duplicate=result.duplicate,
            occurrence_id=occurrence_id,
            run_id=result.run_id,
            code=result.code,
        ).model_dump(),
        headers=_headers(),
    )


__all__ = [
    "RETRY_AFTER_SECONDS",
    "accepted",
    "body_too_large",
    "internal_error",
    "not_found",
    "refused",
    "unsupported_media_type",
]
