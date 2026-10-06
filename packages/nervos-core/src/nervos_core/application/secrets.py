"""Stage H1 Secret Manager: application contracts over encrypted secret storage.

The application layer sees only :class:`SecretMetadata` and Protocols. Plaintext values
enter exactly two operations — create/replace — and are consumed immediately by the
infrastructure adapter that encrypts them; no application value ever holds one, and no
operation returns one. Ciphertext is opaque to this layer.

The ADR 0032 invariants enforced by shape rather than convention:

* no application dataclass carries secret material;
* the key port is a resolver the composition root owns (file-backed, fail-closed);
* lifecycle transitions are owner-scoped and checked transactionally by the adapter;
* revocation destroys ciphertext and is terminal.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from nervos_core.application.clock import Clock
from nervos_core.domain.secrets import (
    InvalidSecret,
    SecretKeyVersion,
    SecretMetadata,
    validate_secret_name,
)


class SecretStoreUnavailable(Exception):
    """The master key is missing, malformed, or rejected: fail closed, never fall back."""


class SecretNotFound(LookupError):
    """The secret does not exist or belongs to another owner."""


class SecretAlreadyExists(ValueError):
    """A secret with the same owner/name already exists."""


class SecretReferenced(ValueError):
    """A lifecycle transition was refused because a connection references the secret."""


class MasterKeyResolver(Protocol):
    """Resolve the current master key and its version, or fail closed.

    The concrete implementation reads the operator-configured key file once per process.
    A resolver that cannot produce a 32-byte key raises :class:`SecretStoreUnavailable`;
    callers must treat that as a hard failure of every secret write and resolution.
    """

    def resolve(self) -> tuple[int, bytes]:
        """Return ``(key_version, key_material)`` or raise SecretStoreUnavailable."""
        ...

    def resolve_version(self, version: int) -> bytes: ...


@dataclass(frozen=True, slots=True)
class SecretWrite:
    """One create/replace command. The plaintext lives only for this call's duration."""

    name: str
    value: str
    provider_hint: str | None = None


class SecretManager:
    """Owner-scoped lifecycle operations over the encrypted secret store."""

    def __init__(
        self,
        persistence: SecretPersistence,
        keys: MasterKeyResolver,
        clock: Clock,
    ) -> None:
        self._persistence = persistence
        self._keys = keys
        self._clock = clock

    def create(self, owner_user_id: int, command: SecretWrite) -> SecretMetadata:
        validate_secret_name(command.name)
        key_version, key = self._keys.resolve()
        return self._persistence.create_secret(
            owner_user_id=owner_user_id,
            name=command.name,
            plaintext=command.value,
            provider_hint=command.provider_hint,
            key_version=key_version,
            key=key,
            now=self._clock(),
        )

    def replace(self, owner_user_id: int, secret_id: int, value: str) -> SecretMetadata:
        key_version, key = self._keys.resolve()
        return self._persistence.replace_secret(
            owner_user_id=owner_user_id,
            secret_id=secret_id,
            plaintext=value,
            key_version=key_version,
            key=key,
            now=self._clock(),
        )

    def inspect(self, owner_user_id: int, secret_id: int) -> SecretMetadata:
        return self._persistence.get_secret(owner_user_id=owner_user_id, secret_id=secret_id)

    def list(self, owner_user_id: int) -> tuple[SecretMetadata, ...]:
        return self._persistence.list_secrets(owner_user_id=owner_user_id)

    def set_status(self, owner_user_id: int, secret_id: int, status: str) -> SecretMetadata:
        """Disable, re-enable, or revoke one owned secret.

        Revocation destroys ciphertext and is refused while any account connection
        references the secret; disabling is refused the same way because a connection
        resolving through a disabled secret fails closed anyway, but the reference check
        keeps the connection's state truthful.
        """
        if status not in ("active", "disabled", "revoked"):
            raise InvalidSecret("secret status is invalid")
        return self._persistence.set_secret_status(
            owner_user_id=owner_user_id, secret_id=secret_id, status=status, now=self._clock()
        )

    def delete(self, owner_user_id: int, secret_id: int) -> None:
        self._persistence.delete_secret(owner_user_id=owner_user_id, secret_id=secret_id)

    def key_versions(self) -> tuple[SecretKeyVersion, ...]:
        """Operator-facing rotation view: which key versions exist and which is active."""
        return self._persistence.list_key_versions()

    def rotate_active_key(self) -> SecretKeyVersion:
        """Record a new active key version; material comes from the resolver's file."""
        version, _ = self._keys.resolve()
        return self._persistence.activate_key_version(key_version=version, now=self._clock())

    def reencrypt_batch(self, limit: int = 100) -> int:
        """Operator-only maintenance; bounded, atomic and safe to repeat."""
        if not 1 <= limit <= 1000:
            raise InvalidSecret("maintenance batch size is invalid")
        version, key = self._keys.resolve()
        return self._persistence.reencrypt_batch(
            key_version=version,
            key=key,
            resolve_key=self._keys.resolve_version,
            limit=limit,
            now=self._clock(),
        )

    def retire_key_version(self, version: int) -> None:
        current, _ = self._keys.resolve()
        if version == current:
            raise SecretReferenced("the configured active key cannot be retired")
        self._persistence.retire_key_version(key_version=version)


class SecretResolver:
    """The Worker-side read path used by the H2 broker.

    This is the only object that turns a secret reference back into plaintext, and it
    exists to make that act *narrow*: one method, one use, no caching, no logging, and a
    fail-closed refusal for anything but an owned, active, decryptable secret. The value
    is returned to the caller and immediately dropped with the call frame.
    """

    def __init__(self, persistence: SecretPersistence, keys: MasterKeyResolver) -> None:
        self._persistence = persistence
        self._keys = keys

    def resolve_active_value(self, owner_user_id: int, secret_id: int) -> str:
        return self._persistence.decrypt_active_value(
            owner_user_id=owner_user_id,
            secret_id=secret_id,
            resolve_key=self._keys.resolve_version,
        )


@dataclass(frozen=True, slots=True)
class ResolvedSecret:
    """A resolved value bounded to one dispatch. Never persisted, never logged."""

    value: str


class SecretPersistence(Protocol):
    """Durable encrypted-secret operations implemented by the SQLAlchemy adapter."""

    def create_secret(
        self,
        *,
        owner_user_id: int,
        name: str,
        plaintext: str,
        provider_hint: str | None,
        key_version: int,
        key: bytes,
        now: datetime,
    ) -> SecretMetadata: ...

    def replace_secret(
        self,
        *,
        owner_user_id: int,
        secret_id: int,
        plaintext: str,
        key_version: int,
        key: bytes,
        now: datetime,
    ) -> SecretMetadata: ...

    def get_secret(self, *, owner_user_id: int, secret_id: int) -> SecretMetadata: ...

    def list_secrets(self, *, owner_user_id: int) -> tuple[SecretMetadata, ...]: ...

    def set_secret_status(
        self, *, owner_user_id: int, secret_id: int, status: str, now: datetime
    ) -> SecretMetadata: ...

    def delete_secret(self, *, owner_user_id: int, secret_id: int) -> None: ...

    def decrypt_active_value(
        self, *, owner_user_id: int, secret_id: int, resolve_key: Callable[[int], bytes]
    ) -> str: ...

    def list_key_versions(self) -> tuple[SecretKeyVersion, ...]: ...

    def activate_key_version(self, *, key_version: int, now: datetime) -> SecretKeyVersion: ...

    def reencrypt_batch(
        self,
        *,
        key_version: int,
        key: bytes,
        resolve_key: Callable[[int], bytes],
        limit: int,
        now: datetime,
    ) -> int: ...

    def retire_key_version(self, *, key_version: int) -> None: ...
