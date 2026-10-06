"""Stage H2 account-connection lifecycle and the credential-broker contract (ADR 0033).

An account connection is *a user's grant to one external provider*, distinct from an MCP
server connection (Stage D) and from an AgentInstance permission. The credential value
lives only in the Secret Manager (ADR 0032); this module carries identity, scopes, and
lifecycle state, and defines the broker port the Worker composes. No module here sees a
token: the broker implementation resolves one inside NervOS's process at dispatch time and
re-checks owner, connection, scope, and Stage-D permission before any external call.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, cast

from nervos_core.application.clock import Clock
from nervos_core.application.secrets import (
    SecretNotFound,
    SecretResolver,
    SecretStoreUnavailable,
)

PROVIDER_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
CONNECTION_STATES = ("connected", "needs_refresh", "disconnected", "revoked")
MAX_SCOPES = 32
MAX_SCOPE_LENGTH = 128


class ConnectionNotFound(LookupError):
    """The connection does not exist or belongs to another owner."""


class InvalidConnection(ValueError):
    """A connection field violates the frozen policy."""


class ConnectionUnavailableError(Exception):
    """Fail-closed outcome for any dispatch attempt that cannot confirm authority.

    The public message is a static sentence: the caller (and any audit record) learns
    *that* authority could not be confirmed, never which predicate failed or why — the
    same disclosure discipline as ADR 0016's connection refusals.
    """

    CODE = "connection_authority_unavailable"
    MESSAGE = "The account connection could not authorize this action."


@dataclass(frozen=True, slots=True)
class AccountConnection:
    """Safe connection view. No field carries, or may ever carry, a token."""

    id: int
    owner_user_id: int
    provider: str
    display_name: str
    secret_id: int
    state: str
    scopes: tuple[str, ...]
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
    refresh_revision: int = 0
    refresh_started_at: datetime | None = None
    revocation_outcome: str | None = None

    def __post_init__(self) -> None:
        if self.state not in CONNECTION_STATES:
            raise InvalidConnection("connection state is invalid")


def _validate_scope(item: object) -> str:
    if (
        not isinstance(item, str)
        or not item
        or len(item) > MAX_SCOPE_LENGTH
        or any(ord(ch) < 33 or ord(ch) == 127 for ch in item)
    ):
        raise InvalidConnection("connection scope is invalid")
    return item


def validate_scopes(scopes: tuple[str, ...]) -> tuple[str, ...]:
    """Apply the same per-scope policy a stored connection must satisfy."""
    if not scopes or len(scopes) > MAX_SCOPES:
        raise InvalidConnection("connection requires between one and thirty-two scopes")
    for scope in scopes:
        _validate_scope(scope)
    return scopes


def parse_scopes(scopes_json: str) -> tuple[str, ...]:
    try:
        decoded: object = json.loads(scopes_json)
    except json.JSONDecodeError as error:
        raise InvalidConnection("connection scopes are malformed") from error
    if not isinstance(decoded, list):
        raise InvalidConnection("connection scopes are malformed")
    return validate_scopes(tuple(_validate_scope(item) for item in cast("list[object]", decoded)))


def canonical_scopes_json(scopes: tuple[str, ...]) -> str:
    return json.dumps(list(scopes), separators=(",", ":"))


class AccountConnectionPersistence(Protocol):
    """Durable, owner-scoped connection operations."""

    def create_connection(
        self,
        *,
        owner_user_id: int,
        provider: str,
        display_name: str,
        secret_id: int,
        scopes: tuple[str, ...],
        expires_at: datetime | None,
        now: datetime,
    ) -> AccountConnection: ...

    def get_connection(self, *, owner_user_id: int, connection_id: int) -> AccountConnection: ...

    def list_connections(self, *, owner_user_id: int) -> tuple[AccountConnection, ...]: ...

    def set_state(
        self, *, owner_user_id: int, connection_id: int, state: str, now: datetime
    ) -> AccountConnection: ...

    def set_expiry(
        self,
        *,
        owner_user_id: int,
        connection_id: int,
        expires_at: datetime | None,
        now: datetime,
    ) -> AccountConnection: ...

    def claim_refresh(
        self, *, owner_user_id: int, connection_id: int, now: datetime
    ) -> AccountConnection | None: ...

    def finish_refresh(
        self,
        *,
        owner_user_id: int,
        connection_id: int,
        revision: int,
        secret_id: int,
        old_secret_rotation: int,
        expires_at: datetime | None,
        scopes: tuple[str, ...],
        now: datetime,
    ) -> bool: ...

    def fail_refresh(
        self, *, owner_user_id: int, connection_id: int, revision: int, now: datetime
    ) -> None: ...

    def record_revocation(
        self, *, owner_user_id: int, connection_id: int, outcome: str, now: datetime
    ) -> None: ...


class AccountConnectionService:
    """Owner-scoped management of durable account-connection configuration."""

    def __init__(
        self,
        persistence: AccountConnectionPersistence,
        clock: Clock,
    ) -> None:
        self._persistence = persistence
        self._clock = clock

    def connect(
        self,
        owner_user_id: int,
        *,
        provider: str,
        display_name: str,
        secret_id: int,
        scopes: tuple[str, ...],
        expires_at: datetime | None = None,
    ) -> AccountConnection:
        if PROVIDER_PATTERN.fullmatch(provider) is None:
            raise InvalidConnection("connection provider is invalid")
        if not display_name or len(display_name) > 128:
            raise InvalidConnection("connection display name is invalid")
        return self._persistence.create_connection(
            owner_user_id=owner_user_id,
            provider=provider,
            display_name=display_name,
            secret_id=secret_id,
            scopes=validate_scopes(scopes),
            expires_at=expires_at,
            now=self._clock(),
        )

    def get(self, owner_user_id: int, connection_id: int) -> AccountConnection:
        return self._persistence.get_connection(
            owner_user_id=owner_user_id, connection_id=connection_id
        )

    def list(self, owner_user_id: int) -> tuple[AccountConnection, ...]:
        return self._persistence.list_connections(owner_user_id=owner_user_id)

    def disconnect(self, owner_user_id: int, connection_id: int) -> AccountConnection:
        """Revoke local authority now. Provider-side revocation is a best-effort follow-up
        the composition layer may attempt; local state changes regardless."""
        return self._persistence.set_state(
            owner_user_id=owner_user_id,
            connection_id=connection_id,
            state="disconnected",
            now=self._clock(),
        )

    def record_revocation(self, owner_user_id: int, connection_id: int, outcome: str) -> None:
        self._persistence.record_revocation(
            owner_user_id=owner_user_id,
            connection_id=connection_id,
            outcome=outcome,
            now=self._clock(),
        )


@dataclass(frozen=True, slots=True)
class BrokerDecision:
    """The typed result of one broker authority check.

    `allowed` carries no credential; a caller that receives `allowed` then asks the
    resolver for the value performs the last-mile read inside its own process.
    """

    allowed: bool
    reason_code: str | None = None


class CredentialBroker:
    """Worker-side authority check for one account action (ADR 0033's five predicates).

    The broker never returns a credential: it answers *whether* an action may proceed and
    callers resolve the value through :class:`SecretResolver` only after `allowed`. Every
    failure is the same safe refusal — never a reason code an attacker could differenate.
    """

    def __init__(
        self,
        connections: AccountConnectionPersistence,
        secrets: SecretResolver,
        clock: Clock,
    ) -> None:
        self._connections = connections
        self._secrets = secrets
        self._clock = clock

    def check(
        self,
        *,
        owner_user_id: int,
        connection_id: int,
        required_scope: str,
    ) -> BrokerDecision:
        try:
            connection = self._connections.get_connection(
                owner_user_id=owner_user_id, connection_id=connection_id
            )
        except ConnectionNotFound:
            return BrokerDecision(False, "connection_missing")
        now = self._clock()
        if connection.state != "connected":
            return BrokerDecision(False, "connection_not_connected")
        if connection.expires_at is not None and connection.expires_at <= now:
            return BrokerDecision(False, "connection_expired")
        if required_scope not in connection.scopes:
            return BrokerDecision(False, "scope_not_granted")
        try:
            self._secrets.resolve_active_value(owner_user_id, connection.secret_id)
        except (SecretNotFound, SecretStoreUnavailable):
            return BrokerDecision(False, "credential_unresolved")
        return BrokerDecision(True)
