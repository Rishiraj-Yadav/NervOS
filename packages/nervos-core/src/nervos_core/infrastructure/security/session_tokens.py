"""Opaque session-token generation and digesting."""

from __future__ import annotations

import hashlib
import re
import secrets

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")


class SecureSessionTokens:
    """Generate 256-bit cookie-safe tokens and binary SHA-256 digests."""

    def generate(self) -> str:
        """Return a fresh URL-safe token with 256 bits of source entropy."""
        return secrets.token_urlsafe(32)

    def digest(self, token: str) -> bytes:
        """Validate and hash a presented token for database lookup."""
        if _TOKEN_PATTERN.fullmatch(token) is None:
            raise ValueError("invalid session token")
        digest = hashlib.sha256(token.encode("ascii")).digest()
        if len(digest) != 32:
            raise RuntimeError("unexpected session digest length")
        return digest
