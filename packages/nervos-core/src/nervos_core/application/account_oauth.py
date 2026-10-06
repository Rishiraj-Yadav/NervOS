"""Provider-neutral account OAuth. Tokens and PKCE verifiers stay server-side."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol
from urllib.parse import urlencode, urlsplit

from nervos_core.application.account_connections import (
    AccountConnection,
    AccountConnectionService,
    InvalidConnection,
    validate_scopes,
)
from nervos_core.application.clock import Clock
from nervos_core.application.secrets import SecretManager, SecretResolver, SecretWrite


class AccountAuthorizationUnavailable(Exception):
    """Static safe error for any rejected OAuth exchange."""


@dataclass(frozen=True, slots=True)
class AccountOAuthProvider:
    id: str
    authorization_endpoint: str
    token_endpoint: str
    client_id: str
    redirect_uri: str
    scopes: tuple[str, ...]
    revocation_endpoint: str | None = None
    client_secret_env: str | None = None

    def __post_init__(self) -> None:
        validate_scopes(self.scopes)
        for endpoint in (
            self.authorization_endpoint,
            self.token_endpoint,
            self.revocation_endpoint,
        ):
            if endpoint is None:
                continue
            parsed = urlsplit(endpoint)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
            ):
                raise InvalidConnection("account provider requires credential-free HTTPS endpoints")
        redirect = urlsplit(self.redirect_uri)
        if (
            redirect.scheme not in ("https", "http")
            or not redirect.hostname
            or redirect.username
            or redirect.password
            or redirect.query
            or redirect.fragment
            or (
                redirect.scheme == "http"
                and redirect.hostname not in ("localhost", "127.0.0.1", "::1")
            )
        ):
            raise InvalidConnection("account redirect URI is invalid")


@dataclass(frozen=True, slots=True)
class AccountTokens:
    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_in: int | None = None
    scopes: tuple[str, ...] = ()

    def sealed_value(self) -> str:
        return json.dumps(
            {"access_token": self.access_token, "refresh_token": self.refresh_token},
            separators=(",", ":"),
        )


class AccountTokenTransport(Protocol):
    async def exchange(
        self, provider: AccountOAuthProvider, form: Mapping[str, str]
    ) -> AccountTokens: ...

    async def revoke(self, provider: AccountOAuthProvider, token: str) -> None: ...


@dataclass(frozen=True, slots=True)
class AccountOAuthRequest:
    owner_user_id: int
    provider: str
    secret_id: int
    scopes: tuple[str, ...]
    display_name: str


class AccountOAuthPersistence(Protocol):
    def create(
        self, *, state_hash: str, request: AccountOAuthRequest, expires_at: datetime, now: datetime
    ) -> None: ...

    def claim(
        self, *, owner_user_id: int, state_hash: str, now: datetime
    ) -> AccountOAuthRequest: ...


class AccountOAuthService:
    def __init__(
        self,
        *,
        providers: Mapping[str, AccountOAuthProvider],
        persistence: AccountOAuthPersistence,
        secrets_manager: SecretManager,
        resolver: SecretResolver,
        connections: AccountConnectionService,
        transport: AccountTokenTransport,
        clock: Clock,
    ) -> None:
        self._providers = providers
        self._store = persistence
        self._manager = secrets_manager
        self._resolver = resolver
        self._connections = connections
        self._transport = transport
        self._clock = clock

    def providers(self) -> tuple[AccountOAuthProvider, ...]:
        return tuple(self._providers.values())

    def start(
        self, owner_user_id: int, *, provider_id: str, scopes: tuple[str, ...], display_name: str
    ) -> str:
        provider = self._providers.get(provider_id)
        if provider is None or not set(validate_scopes(scopes)) <= set(provider.scopes):
            raise AccountAuthorizationUnavailable
        if not display_name.strip() or len(display_name) > 128:
            raise InvalidConnection("account display name is invalid")
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        metadata = self._manager.create(
            owner_user_id,
            SecretWrite(
                f"oauth-pkce-{secrets.token_hex(16)}",
                verifier,
                provider.id,
            ),
        )
        now = self._clock()
        self._store.create(
            state_hash=hashlib.sha256(state.encode()).hexdigest(),
            request=AccountOAuthRequest(
                owner_user_id, provider.id, metadata.id, scopes, display_name
            ),
            expires_at=now + timedelta(minutes=10),
            now=now,
        )
        return (
            provider.authorization_endpoint
            + ("&" if "?" in provider.authorization_endpoint else "?")
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": provider.client_id,
                    "redirect_uri": provider.redirect_uri,
                    "scope": " ".join(scopes),
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )
        )

    async def finish(self, owner_user_id: int, *, state: str, code: str) -> AccountConnection:
        if not 1 <= len(code) <= 4096 or not 32 <= len(state) <= 128:
            raise AccountAuthorizationUnavailable
        request = self._store.claim(
            owner_user_id=owner_user_id,
            state_hash=hashlib.sha256(state.encode()).hexdigest(),
            now=self._clock(),
        )
        provider = self._providers.get(request.provider)
        try:
            if provider is None:
                raise AccountAuthorizationUnavailable
            verifier = self._resolver.resolve_active_value(owner_user_id, request.secret_id)
            tokens = await self._transport.exchange(
                provider,
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "code_verifier": verifier,
                    "redirect_uri": provider.redirect_uri,
                    "client_id": provider.client_id,
                },
            )
            granted = tokens.scopes or request.scopes
            if not set(granted) <= set(request.scopes):
                raise AccountAuthorizationUnavailable
            credential = self._manager.create(
                owner_user_id,
                SecretWrite(
                    f"oauth-account-{secrets.token_hex(16)}",
                    tokens.sealed_value(),
                    provider.id,
                ),
            )
            return self._connections.connect(
                owner_user_id,
                provider=provider.id,
                display_name=request.display_name,
                secret_id=credential.id,
                scopes=granted,
                expires_at=self._clock() + timedelta(seconds=tokens.expires_in)
                if tokens.expires_in is not None
                else None,
            )
        finally:
            # A claimed verifier is one-use even when the token endpoint fails.
            self._manager.set_status(owner_user_id, request.secret_id, "revoked")

    async def disconnect(self, owner_user_id: int, connection_id: int) -> None:
        """Remove local authority before bounded best-effort provider revocation."""
        connection = self._connections.disconnect(owner_user_id, connection_id)
        provider = self._providers.get(connection.provider)
        if provider is None or provider.revocation_endpoint is None:
            self._connections.record_revocation(owner_user_id, connection_id, "not_supported")
            return
        self._connections.record_revocation(owner_user_id, connection_id, "pending")
        try:
            value = json.loads(
                self._resolver.resolve_active_value(owner_user_id, connection.secret_id)
            )
            token = value.get("refresh_token") or value.get("access_token")
            if isinstance(token, str):
                async with asyncio.timeout(15):
                    await self._transport.revoke(provider, token)
                self._connections.record_revocation(owner_user_id, connection_id, "succeeded")
            else:
                self._connections.record_revocation(owner_user_id, connection_id, "failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            # A remote outage must never restore local authority or expose its payload.
            self._connections.record_revocation(owner_user_id, connection_id, "failed")
