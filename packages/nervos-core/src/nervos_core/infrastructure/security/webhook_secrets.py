"""Opaque webhook locator and secret primitives.

The raw secret is a 256-bit machine token. It is returned once by a later ingress-management
milestone and never persisted here; persistence stores only its SHA-256 digest. The digest is not a
password hash: a 256-bit random token already has enough entropy that a memory-hard KDF would add
work to every delivery without improving the threat model.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

from nervos_core.domain.triggers import (
    WEBHOOK_PUBLIC_ID_LENGTH,
    WEBHOOK_SECRET_DIGEST_LENGTH,
    WEBHOOK_SECRET_LENGTH,
    validate_webhook_public_id,
)

#: The shape of a well-formed secret. This is the **only** home for that predicate: E1 left an
#: unused `_SECRET_PATTERN` in the domain, and duplicating the shape a third time here would let the
#: three drift into disagreeing about what a secret is.
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


def is_well_formed_secret(candidate: str | None) -> bool:
    """Whether a presented credential has the frozen secret shape."""
    return candidate is not None and _TOKEN_PATTERN.fullmatch(candidate) is not None


def secret_digest_for(candidate: str | None) -> bytes:
    """The 32-byte digest to compare for a candidate, **always** one.

    A malformed or absent candidate still yields a fixed-width digest, so the comparison below runs
    the same work whether or not a usable credential was presented. This is what makes the
    unknown-locator and wrong-secret paths do the same amount of digest work.
    """
    if not is_well_formed_secret(candidate):
        return DUMMY_DIGEST
    return digest_secret(candidate)  # pyright: ignore[reportArgumentType]


def secret_matches_digest(candidate_digest: bytes, stored_digest: bytes) -> bool:
    """Compare two 32-byte digests in constant time. `hmac.compare_digest`, never `==`.

    This is deliberately a *pure comparison* and nothing more. It cannot tell whether either side
    was substituted, because a substituted `DUMMY_DIGEST` is a perfectly ordinary 32-byte value --
    so the decision that a dummy must never authorize belongs to :func:`secret_matches`, which
    knows whether a real stored digest existed. Every caller that decides anything must use that
    function, not this one.
    """
    if len(stored_digest) != WEBHOOK_SECRET_DIGEST_LENGTH:
        return False
    if len(candidate_digest) != WEBHOOK_SECRET_DIGEST_LENGTH:
        return False
    return hmac.compare_digest(candidate_digest, stored_digest)


def secret_matches(candidate: str | None, stored_digest: bytes | None) -> bool:
    """The one verification decision for a webhook credential.

    `DUMMY_DIGEST` closes a timing side channel and opens a trap: it stands in both for the stored
    digest of a locator that does not exist *and* for the digest of a credential that is not
    well-formed, so two dummies compare equal and a naive implementation would authenticate an
    unknown endpoint presented with a malformed secret. The comparison is therefore always
    performed -- constant work, as ADR 0019:91 requires -- and its result is conjoined with **two
    independent facts** that a substitution cannot manufacture: a real stored digest existed, and
    the presented credential was well-formed. Neither dummy can satisfy both.
    """
    candidate_digest = secret_digest_for(candidate)
    stored_for_comparison = DUMMY_DIGEST if stored_digest is None else stored_digest
    matched = secret_matches_digest(candidate_digest, stored_for_comparison)
    return matched and stored_digest is not None and is_well_formed_secret(candidate)


__all__ = [
    "DUMMY_DIGEST",
    "digest_secret",
    "generate_public_id",
    "generate_secret",
    "is_well_formed_secret",
    "secret_digest_for",
    "secret_matches",
    "secret_matches_digest",
]
