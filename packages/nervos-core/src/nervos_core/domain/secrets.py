"""Stage H1 domain values for the encrypted Secret Manager.

Every value below is safe to log, serialize into an API response, and render in the
browser: none of them carries plaintext secret material, ciphertext, or key bytes. The
ciphertext itself is an infrastructure concern of the persistence row and is deliberately
absent from this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Durable secret lifecycle. `disabled` is reversible; `revoked` is terminal evidence whose
# ciphertext was destroyed at revocation.
SECRET_STATUSES = ("active", "disabled", "revoked")

MAX_SECRET_NAME_LENGTH = 128
MAX_SECRET_VALUE_BYTES = 8192
# Key version is a small monotonic integer, one row per generated key.
MIN_KEY_VERSION = 1


class InvalidSecret(ValueError):
    """A secret name, value, or status violates the frozen policy."""


def validate_secret_name(name: str) -> str:
    if not name or len(name) > MAX_SECRET_NAME_LENGTH:
        raise InvalidSecret("secret name length is invalid")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise InvalidSecret("secret name contains control characters")
    return name


@dataclass(frozen=True, slots=True)
class SecretMetadata:
    """The non-secret identity of one stored secret.

    This is the only shape that may ever cross an API or UI boundary. There is no field —
    and must never be a field — carrying a value, ciphertext, or key material.
    """

    id: int
    owner_user_id: int
    name: str
    provider_hint: str | None
    status: str
    key_version: int
    rotation_count: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.status not in SECRET_STATUSES:
            raise InvalidSecret("secret status is invalid")
        if self.key_version < MIN_KEY_VERSION:
            raise InvalidSecret("secret key version is invalid")
        if self.rotation_count < 0:
            raise InvalidSecret("secret rotation count is invalid")


@dataclass(frozen=True, slots=True)
class SecretKeyVersion:
    """Non-secret metadata about one master-key version.

    The key *material* lives only in the operator-owned key file; this row exists so a
    stored secret can name the version that encrypted it and rotation can be sequenced.
    """

    key_version: int
    active: bool
    created_at: datetime
