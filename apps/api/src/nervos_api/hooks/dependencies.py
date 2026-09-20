"""Dependencies for the public webhook ingress.

There is deliberately **no** `CurrentUserDependency` and **no** `OriginDependency` here. A login
session is never a webhook credential and a webhook secret never authorizes a management operation,
so this route resolves neither. Its authority comes from the durable trigger row the delivery names,
read inside the transaction that acts on it.

The clock is the same server clock the management API uses: `occurred_at` is the instant the
delivery arrived, and no field of the body can override it.
"""

from __future__ import annotations

from typing import cast

from fastapi import Request
from nervos_core.application.clock import Clock
from nervos_core.application.webhooks import WebhookDeliveryService

from nervos_api.api.dependencies import utc_now


def get_webhook_ingress_service(request: Request) -> WebhookDeliveryService:
    return cast(WebhookDeliveryService, request.app.state.webhook_ingress_service)


def get_webhook_clock() -> Clock:
    """The ingress's server clock. One authority for every instant a delivery records."""
    return utc_now


__all__ = ["get_webhook_clock", "get_webhook_ingress_service"]
