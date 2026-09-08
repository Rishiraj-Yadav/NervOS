"""Argon2id password hashing adapter with bounded expensive work."""

from __future__ import annotations

from threading import BoundedSemaphore

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.profiles import RFC_9106_LOW_MEMORY

from nervos_core.application.authentication import PasswordWorkLimit

# Valid adapter-owned hash for timing-resistant unknown-user verification.
_DUMMY_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$MDEyMzQ1Njc4OWFiY2RlZg$"
    "Xc87xS9rNXSlU47yLkkMuGl3eKDhHY1fFpeMj/dBeN0"
)
_MAX_CONCURRENT_PASSWORD_WORK = 2
_PASSWORD_WORK = BoundedSemaphore(_MAX_CONCURRENT_PASSWORD_WORK)


class Argon2PasswordHasher:
    """Hash and verify passwords with the approved Argon2id profile."""

    def __init__(self, hasher: PasswordHasher | None = None) -> None:
        self._hasher = hasher or PasswordHasher.from_parameters(RFC_9106_LOW_MEMORY)

    def hash(self, password: str) -> str:
        """Return an Argon2id password hash within the process work bound."""
        if not _PASSWORD_WORK.acquire(blocking=False):
            raise PasswordWorkLimit
        try:
            return self._hasher.hash(password)
        finally:
            _PASSWORD_WORK.release()

    def verify(self, password_hash: str, password: str) -> bool:
        """Return false for mismatches and malformed stored hashes."""
        if not _PASSWORD_WORK.acquire(blocking=False):
            raise PasswordWorkLimit
        try:
            return self._verify_without_limit(password_hash, password)
        finally:
            _PASSWORD_WORK.release()

    def verify_dummy(self, password: str) -> None:
        """Perform normal Argon2 work when no stored credential exists."""
        self.verify(_DUMMY_HASH, password)

    def needs_rehash(self, password_hash: str) -> bool:
        """Report whether a successfully verified hash uses old parameters."""
        try:
            return self._hasher.check_needs_rehash(password_hash)
        except InvalidHashError:
            return False

    def _verify_without_limit(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError):
            return False
