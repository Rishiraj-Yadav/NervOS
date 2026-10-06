"""Real execution authority, encrypted custody and fenced refresh; fake remote provider."""

from __future__ import annotations

import asyncio
import base64
import os
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from d6_support import NOW, build_rig, claim, descriptor_named, grant, load_run, revoke, submit
from nervos_core.application.account_actions import AccountActionBroker
from nervos_core.application.account_connections import ConnectionUnavailableError
from nervos_core.application.account_oauth import AccountOAuthProvider, AccountTokens
from nervos_core.application.builtin_tools import BUILTIN_SOURCE_REF
from nervos_core.application.secrets import SecretManager, SecretResolver, SecretWrite
from nervos_core.application.tool_registry import ToolResult
from nervos_core.infrastructure.database.account_authority import SqlAlchemyAccountDispatchAuthority
from nervos_core.infrastructure.database.account_connections import (
    SqlAlchemyAccountConnectionPersistence,
)
from nervos_core.infrastructure.database.secrets import SqlAlchemySecretPersistence
from nervos_core.infrastructure.database.tool_definitions import SqlAlchemyToolDefinitionPersistence
from nervos_core.infrastructure.security.secret_keys import FileMasterKeyResolver
from nervos_mcp.connectors.gmail import GMAIL_API_ORIGIN, GMAIL_READONLY_SCOPE, HttpGmailTransport
from nervos_mcp.connectors.gmail_registry import register_gmail_reads
from nervos_mcp.gateway import McpGateway
from nervos_mcp.policy.egress import StrictEgressPolicy
from nervos_worker.account_actions import AccountToolBinding, WorkerAccountActionDispatcher


@pytest.mark.parametrize(
    "scenario", ["refresh", "concurrent", "disconnect", "grant", "scope", "echo"]
)
def test_account_dispatch_refresh_and_live_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    rig = build_rig(tmp_path, monkeypatch)
    descriptor = descriptor_named(rig, "calculate")
    grant(rig, descriptor=descriptor)
    run_id = submit(rig)
    handle = claim(rig, run_id)
    run = load_run(rig, run_id)
    key = tmp_path / "synthetic.key"
    key.write_bytes(base64.b64encode(os.urandom(32)))
    os.chmod(key, 0o600)
    store = SqlAlchemySecretPersistence(rig.engine)
    keys = FileMasterKeyResolver(key, 1)
    manager = SecretManager(store, keys, lambda: NOW)
    connections = SqlAlchemyAccountConnectionPersistence(rig.engine)
    secret = manager.create(
        1,
        SecretWrite(
            "fixture-account", AccountTokens("old-access", "old-refresh").sealed_value(), "example"
        ),
    )
    account = connections.create_connection(
        owner_user_id=1,
        provider="example",
        display_name="Example",
        secret_id=secret.id,
        scopes=("read",),
        expires_at=NOW - timedelta(seconds=1),
        now=NOW,
    )
    provider = AccountOAuthProvider(
        "example",
        "https://example.test/authorize",
        "https://example.test/token",
        "public",
        "http://localhost/callback",
        ("read",),
    )

    class Tokens:
        calls = 0

        async def exchange(
            self, provider: AccountOAuthProvider, form: Mapping[str, str]
        ) -> AccountTokens:
            assert provider.id == "example"
            assert form["refresh_token"] == "old-refresh"
            self.calls += 1
            await asyncio.sleep(0.01)
            if scenario == "disconnect":
                connections.set_state(
                    owner_user_id=1, connection_id=account.id, state="disconnected", now=NOW
                )
            if scenario == "grant":
                revoke(rig, descriptor=descriptor)
            return AccountTokens("new-access", "new-refresh", 3600, ("read",))

        async def revoke(self, provider: AccountOAuthProvider, token: str) -> None:
            return None

    tokens = Tokens()
    broker = AccountActionBroker(
        connections=connections,
        manager=manager,
        resolver=SecretResolver(store, keys),
        providers={"example": provider},
        transport=tokens,
        authority=SqlAlchemyAccountDispatchAuthority(rig.engine),
        clock=lambda: NOW,
    )
    dispatched: list[str] = []

    async def execute(token: str) -> ToolResult:
        dispatched.append(token)
        return ToolResult("new-access" if scenario == "echo" else "safe external result")

    async def dispatch() -> ToolResult:
        return await broker.dispatch(
            run=run,
            claim=handle,
            descriptor=descriptor,
            connection_id=account.id,
            required_scope="write" if scenario == "scope" else "read",
            execute=execute,
        )

    async def exercise() -> None:
        if scenario == "concurrent":
            results = await asyncio.gather(dispatch(), dispatch())
            assert len(results) == 2
        else:
            await dispatch()

    if scenario in ("disconnect", "grant", "scope", "echo"):
        with pytest.raises(ConnectionUnavailableError):
            asyncio.run(exercise())
        assert dispatched == (["new-access"] if scenario == "echo" else [])
    else:
        asyncio.run(exercise())
        assert dispatched == ["new-access"] * (2 if scenario == "concurrent" else 1)
        assert manager.inspect(1, secret.id).status == "revoked"
    assert tokens.calls == (0 if scenario == "scope" else 1)


@pytest.mark.parametrize("scenario", ["success", "revoke", "scope", "fingerprint", "disconnect"])
def test_native_gmail_dispatch_uses_encrypted_broker_and_live_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    rig = build_rig(tmp_path, monkeypatch)
    definitions = SqlAlchemyToolDefinitionPersistence(rig.engine)
    register_gmail_reads(definitions, lambda: NOW)
    descriptor = next(
        p.to_descriptor()
        for p in definitions.list_for_source(source_ref=BUILTIN_SOURCE_REF)
        if p.material.upstream_name == "gmail.users.messages.list"
    )
    grant(rig, descriptor=descriptor)
    run_id = submit(rig)
    handle, run = claim(rig, run_id), load_run(rig, run_id)
    key = tmp_path / "synthetic-gmail.key"
    key.write_bytes(base64.b64encode(os.urandom(32)))
    os.chmod(key, 0o600)
    store = SqlAlchemySecretPersistence(rig.engine)
    keys = FileMasterKeyResolver(key, 1)
    manager = SecretManager(store, keys, lambda: NOW)
    connections = SqlAlchemyAccountConnectionPersistence(rig.engine)
    secret = manager.create(
        1,
        SecretWrite(
            "gmail", AccountTokens("fixture-access", "fixture-refresh").sealed_value(), "google"
        ),
    )
    scopes = ("wrong",) if scenario == "scope" else (GMAIL_READONLY_SCOPE,)
    account = connections.create_connection(
        owner_user_id=1,
        provider="google",
        display_name="Fixture Gmail",
        secret_id=secret.id,
        scopes=scopes,
        expires_at=NOW + timedelta(hours=1),
        now=NOW,
    )
    provider = AccountOAuthProvider(
        "google",
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://oauth2.googleapis.com/token",
        "fixture-client",
        "http://localhost/callback",
        (GMAIL_READONLY_SCOPE,),
    )

    class Tokens:
        async def exchange(
            self, provider: AccountOAuthProvider, form: Mapping[str, str]
        ) -> AccountTokens:
            raise AssertionError("unexpired token must not refresh")

        async def revoke(self, provider: AccountOAuthProvider, token: str) -> None:
            return None

    broker = AccountActionBroker(
        connections=connections,
        manager=manager,
        resolver=SecretResolver(store, keys),
        providers={"google": provider},
        transport=Tokens(),
        authority=SqlAlchemyAccountDispatchAuthority(rig.engine),
        clock=lambda: NOW,
    )
    calls: list[str] = []

    async def read(self: HttpGmailTransport, url: str, *, access_token: str) -> tuple[int, object]:
        assert access_token == "fixture-access"
        calls.append(url)
        return 200, {"messages": [{"id": "m1", "threadId": "t1"}]}

    monkeypatch.setattr(HttpGmailTransport, "get_json", read)
    binding = AccountToolBinding(
        tool_definition_id=descriptor.tool_definition_id,
        account_connection_id=account.id,
        fingerprint="0" * 64 if scenario == "fingerprint" else descriptor.fingerprint,
        required_scope=GMAIL_READONLY_SCOPE,
        endpoint=GMAIL_API_ORIGIN,
    )
    dispatcher = WorkerAccountActionDispatcher(
        broker,
        cast(McpGateway, None),
        (binding,),
        StrictEgressPolicy(allowed_origins=frozenset({GMAIL_API_ORIGIN})),
    )
    if scenario == "revoke":
        revoke(rig, descriptor=descriptor)
    if scenario == "disconnect":
        connections.set_state(
            owner_user_id=1, connection_id=account.id, state="disconnected", now=NOW
        )

    async def dispatch() -> ToolResult:
        return await dispatcher.invoke(run=run, claim=handle, descriptor=descriptor, arguments={})

    if scenario == "success":
        result = asyncio.run(dispatch())
        assert "m1" in str(result.structured)
        assert "fixture-access" not in str(result)
        assert len(calls) == 1
    else:
        from nervos_core.application.tool_registry import ToolExecutionFailure

        with pytest.raises(ToolExecutionFailure):
            asyncio.run(dispatch())
        assert calls == []
    rig.engine.dispose()
