"""Hosted identities and assurance. Independent of local NervOS accounts."""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from nervos_marketplace_service.domain.errors import MarketplaceError

type Record = dict[str, Any]


def now() -> datetime:
    return datetime.now(UTC)


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def pkce(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", value) is None:
        raise MarketplaceError("invalid_request")
    return base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest()).decode().rstrip("=")


@dataclass(frozen=True)
class Actor:
    account_id: UUID
    authenticated_at: datetime
    acr: str | None
    amr: tuple[str, ...]
    scope: str
    credential_hash: str = field(repr=False)
    publisher_id: UUID | None = None


@dataclass(frozen=True)
class Assurance:
    acr_values: tuple[str, ...] = ()
    required_amr: tuple[str, ...] = ()
    max_age_seconds: int = 300

    def require(self, actor: Actor) -> None:
        age = (now() - actor.authenticated_at).total_seconds()
        configured = bool(self.acr_values or self.required_amr)
        acr_ok = not self.acr_values or actor.acr in self.acr_values
        amr_ok = not self.required_amr or set(self.required_amr).issubset(actor.amr)
        if not configured or not acr_ok or not amr_ok or not 0 <= age <= self.max_age_seconds:
            raise MarketplaceError("recent_auth_required", 403)


def publisher_handle(value: str) -> str:
    if re.fullmatch(r"[a-z][a-z0-9-]{1,61}[a-z0-9]", value) is None or value in {
        "nervos",
        "admin",
        "system",
        "operator",
        "marketplace",
    }:
        raise MarketplaceError("invalid_request")
    return value


def bounded_text(value: str, characters: int, size: int, *, nonempty: bool = False) -> str:
    if len(value) > characters or len(value.encode()) > size or (nonempty and not value.strip()):
        raise MarketplaceError("invalid_request")
    return value
