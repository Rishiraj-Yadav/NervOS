"""Last-mile account credential custody for a mediated external tool invocation."""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from typing import Protocol, cast

from nervos_core.application.account_connections import (
    AccountConnection,
    AccountConnectionPersistence,
    ConnectionUnavailableError,
)
from nervos_core.application.account_oauth import (
    AccountOAuthProvider,
    AccountTokens,
    AccountTokenTransport,
)
from nervos_core.application.clock import Clock
from nervos_core.application.secrets import SecretManager, SecretResolver, SecretWrite
from nervos_core.application.tool_invocations import ClaimHandle
from nervos_core.application.tool_registry import ToolResult
from nervos_core.domain.runs import Run
from nervos_core.domain.tools import JsonValue, ToolDescriptor, canonical_json_text


class AccountDispatchAuthority(Protocol):
    def authorize(
        self, *, run: Run, claim: ClaimHandle, descriptor: ToolDescriptor, now: datetime
    ) -> int: ...


class AccountActionDispatcher(Protocol):
    def applies(self, descriptor: ToolDescriptor) -> bool: ...

    async def invoke(
        self,
        *,
        run: Run,
        claim: ClaimHandle,
        descriptor: ToolDescriptor,
        arguments: Mapping[str, JsonValue],
    ) -> ToolResult: ...


def _tokens(value: str) -> AccountTokens:
    if value.startswith("{"):
        row = cast(dict[str, object], json.loads(value))
        access, refresh = row.get("access_token"), row.get("refresh_token")
    else:
        access, refresh = value, None  # Explicit manually provisioned bearer credential.
    if (
        not isinstance(access, str)
        or not 1 <= len(access) <= 3072
        or any(ord(ch) <= 32 or ord(ch) >= 127 for ch in access)
        or (refresh is not None and not isinstance(refresh, str))
    ):
        raise ConnectionUnavailableError
    return AccountTokens(access, refresh)


class AccountActionBroker:
    def __init__(
        self,
        *,
        connections: AccountConnectionPersistence,
        manager: SecretManager,
        resolver: SecretResolver,
        providers: Mapping[str, AccountOAuthProvider],
        transport: AccountTokenTransport,
        authority: AccountDispatchAuthority,
        clock: Clock,
    ) -> None:
        self._connections = connections
        self._manager = manager
        self._resolver = resolver
        self._providers = providers
        self._transport = transport
        self._authority = authority
        self._clock = clock

    async def dispatch(
        self,
        *,
        run: Run,
        claim: ClaimHandle,
        descriptor: ToolDescriptor,
        connection_id: int,
        required_scope: str,
        execute: Callable[[str], Awaitable[ToolResult]],
    ) -> ToolResult:
        try:
            owner = self._authority.authorize(
                run=run, claim=claim, descriptor=descriptor, now=self._clock()
            )
            connection = self._connections.get_connection(
                owner_user_id=owner, connection_id=connection_id
            )
            if required_scope not in connection.scopes:
                raise ConnectionUnavailableError
            # Bounded single-flight refresh; no DB transaction spans an await.
            async with asyncio.timeout(20):
                connection = await self._fresh(owner, connection)
            owner = self._authority.authorize(
                run=run, claim=claim, descriptor=descriptor, now=self._clock()
            )
            connection = self._connections.get_connection(
                owner_user_id=owner, connection_id=connection_id
            )
            if (
                connection.state != "connected"
                or required_scope not in connection.scopes
                or (connection.expires_at is not None and connection.expires_at <= self._clock())
            ):
                raise ConnectionUnavailableError
            tokens = _tokens(self._resolver.resolve_active_value(owner, connection.secret_id))
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ConnectionUnavailableError from None
        # Entering execute is the external-effect boundary: preserve ambiguity rather
        # than catching transport failures and presenting them as known no-dispatch.
        result = await execute(tokens.access_token)
        rendered = result.text + canonical_json_text(result.structured)
        if any(
            value and value in rendered for value in (tokens.access_token, tokens.refresh_token)
        ):
            raise ConnectionUnavailableError  # Refuse credential-shaped remote output.
        return result

    async def _fresh(self, owner: int, connection: AccountConnection) -> AccountConnection:
        while connection.state == "needs_refresh" and connection.refresh_started_at is not None:
            if connection.refresh_started_at + timedelta(seconds=20) <= self._clock():
                raise ConnectionUnavailableError
            await asyncio.sleep(0.1)
            connection = self._connections.get_connection(
                owner_user_id=owner, connection_id=connection.id
            )
        if connection.state != "connected":
            raise ConnectionUnavailableError
        if connection.expires_at is None or connection.expires_at > self._clock():
            return connection
        lease = self._connections.claim_refresh(
            owner_user_id=owner, connection_id=connection.id, now=self._clock()
        )
        if lease is None:
            return await self._fresh(
                owner,
                self._connections.get_connection(owner_user_id=owner, connection_id=connection.id),
            )
        created_id: int | None = None
        try:
            provider = self._providers.get(lease.provider)
            if provider is None:
                raise ConnectionUnavailableError
            metadata = self._manager.inspect(owner, lease.secret_id)
            previous = _tokens(self._resolver.resolve_active_value(owner, lease.secret_id))
            if previous.refresh_token is None:
                raise ConnectionUnavailableError
            tokens = await self._transport.exchange(
                provider,
                {
                    "grant_type": "refresh_token",
                    "refresh_token": previous.refresh_token,
                    "client_id": provider.client_id,
                },
            )
            if tokens.expires_in is None:
                raise ConnectionUnavailableError
            scopes = tokens.scopes or lease.scopes
            if not set(scopes) <= set(lease.scopes):
                raise ConnectionUnavailableError
            # Providers may retain the prior refresh token instead of issuing a new one.
            rotated = AccountTokens(
                tokens.access_token,
                tokens.refresh_token or previous.refresh_token,
                tokens.expires_in,
                scopes,
            )
            created = self._manager.create(
                owner,
                SecretWrite(
                    f"oauth-refresh-{secrets.token_hex(16)}",
                    rotated.sealed_value(),
                    lease.provider,
                ),
            )
            created_id = created.id
            applied = self._connections.finish_refresh(
                owner_user_id=owner,
                connection_id=lease.id,
                revision=lease.refresh_revision,
                secret_id=created.id,
                old_secret_rotation=metadata.rotation_count,
                expires_at=self._clock() + timedelta(seconds=tokens.expires_in),
                scopes=scopes,
                now=self._clock(),
            )
            if not applied:
                raise ConnectionUnavailableError
            created_id = None
            self._manager.set_status(owner, lease.secret_id, "revoked")
            return self._connections.get_connection(owner_user_id=owner, connection_id=lease.id)
        finally:
            if created_id is not None:
                self._manager.set_status(owner, created_id, "revoked")
            self._connections.fail_refresh(
                owner_user_id=owner,
                connection_id=lease.id,
                revision=lease.refresh_revision,
                now=self._clock(),
            )
