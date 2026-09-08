"""Application contracts and policies for local authentication."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

USERNAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.-]{2,31}\Z")
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128
SESSION_LIFETIME_SECONDS = 7 * 24 * 60 * 60


class AuthenticationError(Exception):
    """Base class for expected authentication failures."""


class InvalidUsername(AuthenticationError):
    """Raised when a username cannot satisfy the persisted policy."""


class InvalidPassword(AuthenticationError):
    """Raised when a password does not satisfy the input policy."""


class SetupComplete(AuthenticationError):
    """Raised when initial setup is already permanently unavailable."""


class InvalidCredentials(AuthenticationError):
    """Raised for every public credential-authentication failure."""


class AuthenticationRequired(AuthenticationError):
    """Raised when a session cannot establish an active user."""


class PersistenceUnavailable(AuthenticationError):
    """Raised when SQLite cannot safely complete an authentication operation."""


class PasswordWorkLimit(AuthenticationError):
    """Raised when expensive password work has reached its safe process limit."""


@dataclass(frozen=True, slots=True)
class PublicUser:
    """Safe authenticated-user data available above the application boundary."""

    id: int
    username: str
    role: str
    is_active: bool


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """A committed session and its one-time raw cookie token."""

    user: PublicUser
    token: str
    expires_at: datetime


class PasswordHasher(Protocol):
    """Small application boundary for password hashing."""

    def hash(self, password: str) -> str: ...

    def verify(self, password_hash: str, password: str) -> bool: ...

    def verify_dummy(self, password: str) -> None: ...

    def needs_rehash(self, password_hash: str) -> bool: ...


class SessionTokens(Protocol):
    """Small application boundary for opaque session tokens."""

    def generate(self) -> str: ...

    def digest(self, token: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class StoredCredential:
    """Authentication data retained below the HTTP boundary."""

    user: PublicUser
    password_hash: str


class SessionCollision(AuthenticationError):
    """Raised for an extraordinarily unlikely token-digest collision."""


class Clock(Protocol):
    """Return the current timezone-aware UTC instant."""

    def __call__(self) -> datetime: ...


class AuthenticationPersistence(Protocol):
    """Focused persistence operations required by authentication."""

    def any_user_exists(self) -> bool: ...

    def create_initial_administrator(
        self,
        *,
        username: str,
        password_hash: str,
        token_hash: bytes,
        created_at: datetime,
        expires_at: datetime,
    ) -> PublicUser: ...

    def get_credential(self, username: str) -> StoredCredential | None: ...

    def create_session(
        self,
        *,
        user_id: int,
        token_hash: bytes,
        created_at: datetime,
        expires_at: datetime,
        replacement_password_hash: str | None,
    ) -> PublicUser: ...

    def resolve_user(self, token_hash: bytes, now: datetime) -> PublicUser | None: ...

    def revoke_session(self, token_hash: bytes, revoked_at: datetime) -> None: ...


class AuthenticationService:
    """Coordinate setup, credentials, and opaque server-side sessions."""

    def __init__(
        self,
        persistence: AuthenticationPersistence,
        passwords: PasswordHasher,
        tokens: SessionTokens,
        clock: Clock,
    ) -> None:
        self._persistence = persistence
        self._passwords = passwords
        self._tokens = tokens
        self._clock = clock

    def setup(self, username: str, password: str) -> IssuedSession:
        """Create the first administrator and its initial session atomically."""
        canonical_username = canonicalize_username(username)
        validated_password = validate_password(password)
        if self._persistence.any_user_exists():
            raise SetupComplete
        password_hash = self._passwords.hash(validated_password)
        now = self._clock()
        expires_at = _session_expiration(now)
        token = self._tokens.generate()
        token_hash = self._tokens.digest(token)
        user = self._persistence.create_initial_administrator(
            username=canonical_username,
            password_hash=password_hash,
            token_hash=token_hash,
            created_at=now,
            expires_at=expires_at,
        )
        return IssuedSession(user=user, token=token, expires_at=expires_at)

    def login(self, username: str, password: str) -> IssuedSession:
        """Authenticate credentials and create a fresh absolute-expiry session."""
        canonical_username = canonicalize_username(username)
        validated_password = validate_password(password)
        credential = self._persistence.get_credential(canonical_username)
        if credential is None:
            self._passwords.verify_dummy(validated_password)
            raise InvalidCredentials

        verified = self._passwords.verify(credential.password_hash, validated_password)
        if not verified or not credential.user.is_active:
            raise InvalidCredentials

        replacement_hash = None
        if self._passwords.needs_rehash(credential.password_hash):
            replacement_hash = self._passwords.hash(validated_password)

        now = self._clock()
        expires_at = _session_expiration(now)
        for _ in range(3):
            token = self._tokens.generate()
            token_hash = self._tokens.digest(token)
            try:
                user = self._persistence.create_session(
                    user_id=credential.user.id,
                    token_hash=token_hash,
                    created_at=now,
                    expires_at=expires_at,
                    replacement_password_hash=replacement_hash,
                )
            except SessionCollision:
                continue
            return IssuedSession(user=user, token=token, expires_at=expires_at)
        raise PersistenceUnavailable

    def authenticate(self, token: str) -> PublicUser:
        """Resolve one valid opaque token to its active persisted user."""
        try:
            token_hash = self._tokens.digest(token)
        except ValueError as error:
            raise AuthenticationRequired from error
        user = self._persistence.resolve_user(token_hash, self._clock())
        if user is None:
            raise AuthenticationRequired
        return user

    def logout(self, token: str | None) -> None:
        """Idempotently revoke the current session when a valid token is present."""
        if token is None:
            return
        try:
            token_hash = self._tokens.digest(token)
        except ValueError:
            return
        self._persistence.revoke_session(token_hash, self._clock())


def _session_expiration(created_at: datetime) -> datetime:
    """Return the fixed seven-day absolute session expiration."""
    from datetime import timedelta

    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("authentication clock must return an aware datetime")
    return created_at + timedelta(seconds=SESSION_LIFETIME_SECONDS)


def canonicalize_username(username: str) -> str:
    """Normalize a username into the only accepted persisted form."""
    canonical = unicodedata.normalize("NFKC", username).strip().lower()
    if USERNAME_PATTERN.fullmatch(canonical) is None:
        raise InvalidUsername
    return canonical


def validate_password(password: str) -> str:
    """Enforce bounded password input without normalizing or truncating it."""
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise InvalidPassword
    return password
