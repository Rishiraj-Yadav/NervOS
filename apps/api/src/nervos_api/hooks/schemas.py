"""The webhook ingress's public response models.

Both are explicit allow-lists rather than serializations of anything stored, so a field can only
appear here by being written here. Nothing an operator or an agent owns is reachable: no owner id,
no Agent Instance or Agent Definition identity, no Job id, no Attempt id, no claim token, no lease,
no worker identity, no partition, no provider or model name, no grant information, no secret digest,
no payload, and no raw `Idempotency-Key`.
"""

from __future__ import annotations

from pydantic import BaseModel


class WebhookAcceptedResponse(BaseModel):
    """What an accepted delivery publishes.

    ``occurrence_id`` is the durable handle for the delivery itself; ``run_id`` is present only when
    materialization completed, so it is null for a delivery that was evaluated and deliberately
    produced no Run. ``code`` names *why* in a frozen, static vocabulary, and is null when a Run
    exists.
    """

    duplicate: bool
    occurrence_id: int
    run_id: int | None
    code: str | None


class WebhookErrorResponse(BaseModel):
    """What a refusal publishes: a static code and a static message, and nothing else."""

    code: str
    message: str


__all__ = ["WebhookAcceptedResponse", "WebhookErrorResponse"]
