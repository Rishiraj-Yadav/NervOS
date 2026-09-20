"""The public webhook ingress route: one method, one path, one ordering.

The ordering is ADR 0019's, and it preserves two properties at once. The request body's memory and
I/O are bounded **before any database work**, because the ADR freezes the bound as applying "before
parsing and before any database work". And no JSON or payload semantics are applied **before the
credential is settled**, so an unauthenticated caller learns nothing about whether their body would
have been acceptable, and no parsing work is spent on their behalf.

The namespace is a sibling of `/api/v1`, not a branch inside it. This module does not touch the
management router or its middleware, and the `Origin`/CSRF boundary that protects the browser API is
scoped by path prefix, so adding this route weakened nothing.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from nervos_core.application.clock import Clock
from nervos_core.application.webhooks import (
    WebhookDelivery,
    WebhookDeliveryKind,
    WebhookDeliveryService,
)
from nervos_core.domain.webhooks import WEBHOOK_BODY_MAX_BYTES, is_well_formed_public_id
from starlette.responses import Response

from nervos_api.hooks import errors
from nervos_api.hooks.dependencies import get_webhook_clock, get_webhook_ingress_service

#: The ingress is not the management contract, so it is deliberately absent from the OpenAPI
#: document: publishing an unauthenticated mutation endpoint into the artifact the dashboard's
#: consumers read would describe a contract they are not party to.
router = APIRouter(prefix="/hooks/v1", include_in_schema=False)

_AUTHORIZATION = b"authorization"
_CONTENT_LENGTH = b"content-length"
_CONTENT_TYPE = b"content-type"
_IDEMPOTENCY_KEY = b"idempotency-key"

#: The kinds that publish the accepted shape. The complement is a refusal.
_ACCEPTED = frozenset(
    {
        WebhookDeliveryKind.MATERIALIZED,
        WebhookDeliveryKind.SKIPPED,
        WebhookDeliveryKind.DUPLICATE,
    }
)


@router.post("/{public_id}")
async def deliver_webhook(
    public_id: str,
    request: Request,
    service: Annotated[WebhookDeliveryService, Depends(get_webhook_ingress_service)],
    clock: Annotated[Clock, Depends(get_webhook_clock)],
) -> Response:
    """Deliver one event to one webhook trigger.

    The steps below are the frozen order, and each comment says which property it exists to hold.
    The value returned is always one of the ingress's own static responses, so nothing a caller
    sends can be reflected back and no internal reason can escape.
    """
    # 1. The locator's shape, and nothing more. A malformed locator can never authenticate, so it
    #    gets the one authentication-failure answer immediately; no database work is ordered
    #    against it either, because there is none to order.
    if not is_well_formed_public_id(public_id):
        return errors.not_found()

    # 2. The credential's syntax. Two `Authorization` headers are an ambiguity, and the ingress
    #    refuses the ambiguity rather than choosing one of them. A missing, wrongly-schemed or
    #    empty credential is not a syntax error -- it is simply no credential, and it flows on to
    #    the authentication step so that every failure produces the same answer.
    authorization = _header_values(request, _AUTHORIZATION)
    if len(authorization) > 1:
        return errors.not_found()
    candidate_secret = _bearer_token(authorization[0]) if authorization else None

    # 3. A cheap early refusal on a *declared* length. It is only a hint -- it can be absent,
    #    duplicated, negative or false -- so it never replaces the enforced bound below.
    declared_length = _declared_length(request)
    if declared_length is not None and declared_length > WEBHOOK_BODY_MAX_BYTES:
        return errors.body_too_large()

    # 4. The authoritative bound. Read at most the frozen maximum and stop the moment it is
    #    exceeded; do no JSON work here. This is the last step common to every request, which is
    #    what keeps body memory and I/O bounded before any database work without letting an
    #    unauthenticated caller reach the parser.
    raw_body = await _read_bounded(request)
    if raw_body is None:
        return errors.body_too_large()

    # 5-10. Authenticate, then interpret the body, then materialize -- all of it in the service,
    #    whose transaction re-checks the credential against the digest that exists at the moment
    #    the work is actually done.
    try:
        result = service.receive(
            WebhookDelivery(
                public_id=public_id,
                candidate_secret=candidate_secret,
                idempotency_key=_single_header(request, _IDEMPOTENCY_KEY),
                has_conflicting_idempotency_keys=len(_header_values(request, _IDEMPOTENCY_KEY)) > 1,
                is_json_content_type=_is_json_content_type(request),
                occurred_at=clock(),
            ),
            raw_body,
        )
    except Exception:
        # The ingress owes its callers *its own* response contract. Without this, an unexpected
        # failure would be answered by the management API's envelope, which is a different shape
        # describing a surface this caller is not party to. Nothing is logged here: a raw exception
        # may carry a payload or a credential, and Stage E never logs either.
        return errors.internal_error()

    if result.kind in _ACCEPTED:
        return errors.accepted(result)
    return errors.refused(result)


def _header_values(request: Request, name: bytes) -> list[str]:
    """Every value for one header name, without collapsing duplicates.

    `request.headers` keeps a single value per name, and a request carrying two credentials -- or
    two idempotency keys -- is exactly the ambiguity this ingress must refuse rather than resolve,
    so the raw ASGI list is what is read. ASGI header names are already lowercase.
    """
    return [
        value.decode("latin-1") for key, value in request.scope["headers"] if key.lower() == name
    ]


def _single_header(request: Request, name: bytes) -> str | None:
    """One header's value as a trimmed string, or ``None`` when it is absent or repeated."""
    values = _header_values(request, name)
    if len(values) != 1:
        return None
    return values[0].strip() or None


def _bearer_token(value: str) -> str | None:
    """The secret from a `Bearer` credential, or ``None`` when the header presents no usable one.

    Strict on purpose: a wrong scheme, a missing separator and an empty token all yield ``None``.
    The secret is accepted **only** from this header -- never from a query string, a path segment,
    the body, or a cookie.
    """
    scheme, separator, token = value.strip().partition(" ")
    if separator != " " or scheme.lower() != "bearer":
        return None
    return token.strip() or None


def _declared_length(request: Request) -> int | None:
    """The declared body length, or ``None`` when it is absent, repeated or unusable."""
    values = _header_values(request, _CONTENT_LENGTH)
    if len(values) != 1:
        return None
    try:
        declared = int(values[0])
    except ValueError:
        return None
    return declared if declared >= 0 else None


def _is_json_content_type(request: Request) -> bool:
    """Whether the request declares exactly one JSON content type.

    Carried as a *fact* into the service rather than acted on here, because the frozen ordering
    refuses a non-JSON body only after authentication has succeeded.
    """
    values = _header_values(request, _CONTENT_TYPE)
    if len(values) != 1:
        return False
    media_type, *parameters = values[0].split(";")
    if media_type.strip().lower() != "application/json":
        return False
    return all("=" in parameter and bool(parameter.strip()) for parameter in parameters)


async def _read_bounded(request: Request) -> bytes | None:
    """Read at most the frozen maximum, or ``None`` the moment it is exceeded.

    The bytes actually counted are what is enforced, never the declared length. The read stops as
    soon as the bound is exceeded rather than completing and then measuring, so an over-size body
    cannot be buffered in full.
    """
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > WEBHOOK_BODY_MAX_BYTES:
            return None
    return bytes(body)


__all__ = ["router"]
