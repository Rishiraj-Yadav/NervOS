"""Opaque webhook locator and secret primitives.

The raw secret is a 256-bit machine token. It is returned once by a later ingress-management
milestone and never persisted here; persistence stores only its SHA-256 digest. The digest is not a
password hash: a 256-bit random token already has enough entropy that a memory-hard KDF would add
work to every delivery without improving the threat model.
"""

from __future__ import annotations

import hashlib
import re
import secrets

from nervos_core.domain.triggers import (
    WEBHOOK_PUBLIC_ID_LENGTH,
    WEBHOOK_SECRET_DIGEST_LENGTH,
    WEBHOOK_SECRET_LENGTH,
    validate_webhook_public_id,
)

_TOKEN_PATTERN = re.compile(rf"[A-Za-z0-9_-]{{{WEBHOOK_SECRET_LENGTH}}}\Z")
DUMMY_DIGEST = bytes(WEBHOOK_SECRET_DIGEST_LENGTH)


def generate_public_id() -> str:
    """Generate the opaque public locator, separate from the credential."""
    value = secrets.token_urlsafe(16)
    if len(value) != WEBHOOK_PUBLIC_ID_LENGTH:
        raise RuntimeError("unexpected webhook public id length")
    validate_webhook_public_id(value)
    return value


def generate_secret() -> str:
    """Generate a 256-bit URL-safe webhook secret."""
    value = secrets.token_urlsafe(32)
    if _TOKEN_PATTERN.fullmatch(value) is None:
        raise RuntimeError("unexpected webhook secret shape")
    return value


def digest_secret(secret: str) -> bytes:
    """Return the fixed-width digest stored in a TriggerDefinition."""
    if _TOKEN_PATTERN.fullmatch(secret) is None:
        raise ValueError("invalid webhook secret")
    digest = hashlib.sha256(secret.encode("ascii")).digest()
    if len(digest) != WEBHOOK_SECRET_DIGEST_LENGTH:
        raise RuntimeError("unexpected webhook secret digest length")
    return digest
