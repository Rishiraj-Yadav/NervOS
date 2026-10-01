"""Untrusted navigation cursors grant no authority and need no signing secret."""

import base64
import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nervos_marketplace_service.application.ports import CatalogPosition
from nervos_marketplace_service.domain.catalog import identity
from nervos_marketplace_service.domain.errors import MarketplaceError


class Cursor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    v: int = Field(default=1, ge=1, le=1)
    kind: str
    binding: str
    exact: int = Field(ge=0, le=1)
    rank: int = Field(ge=0, le=9223372036854775807)
    package_id: str = Field(max_length=128)
    version: str = Field(max_length=64)


def fingerprint(kind: str, query: str, unavailable: bool = False, prerelease: bool = True) -> str:
    return hashlib.sha256(
        json.dumps([kind, query, unavailable, prerelease], ensure_ascii=True).encode()
    ).hexdigest()


def encode(position: CatalogPosition, kind: str, binding: str) -> str:
    value = Cursor(
        kind=kind,
        binding=binding,
        exact=position.exact,
        rank=position.rank,
        package_id=position.package_id,
        version=position.version,
    )
    return base64.urlsafe_b64encode(value.model_dump_json().encode()).decode().rstrip("=")


def decode(raw: str | None, kind: str, binding: str) -> CatalogPosition | None:
    if raw is None:
        return None
    try:
        if not raw or len(raw) > 2048:
            raise ValueError
        data = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
        cursor = Cursor.model_validate_json(data)
        if cursor.kind != kind or cursor.binding != binding:
            raise ValueError
        identity(cursor.package_id, cursor.version if kind == "versions" else None)
        if kind == "packages" and cursor.version:
            raise ValueError
        return CatalogPosition(cursor.exact, cursor.rank, cursor.package_id, cursor.version)
    except (ValueError, ValidationError):
        raise MarketplaceError("invalid_request") from None
