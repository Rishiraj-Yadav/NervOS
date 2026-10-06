"""Real durable OAuth custody with a fake provider; no live credentials or services."""

from __future__ import annotations

import asyncio
import base64
import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from execution_support import NOW, migrate
from nervos_core.application.account_connections import AccountConnectionService
from nervos_core.application.account_oauth import (
    AccountAuthorizationUnavailable,
    AccountOAuthProvider,
    AccountOAuthService,
    AccountTokens,
)
from nervos_core.application.secrets import SecretManager, SecretResolver
from nervos_core.infrastructure.database.account_connections import (
    SqlAlchemyAccountConnectionPersistence,
)
from nervos_core.infrastructure.database.account_oauth import SqlAlchemyAccountOAuthPersistence
from nervos_core.infrastructure.database.secrets import SqlAlchemySecretPersistence
from nervos_core.infrastructure.security.secret_keys import FileMasterKeyResolver
from sqlalchemy import text


class FakeTokens:
    def __init__(self) -> None:
        self.calls = 0

    async def exchange(
        self, provider: AccountOAuthProvider, form: Mapping[str, str]
    ) -> AccountTokens:
        assert form["grant_type"] == "authorization_code"
        assert 43 <= len(form["code_verifier"]) <= 128
        assert provider.id == "example"
        self.calls += 1
        return AccountTokens("synthetic-access-token", "synthetic-refresh-token", 3600, ("read",))

    async def revoke(self, provider: AccountOAuthProvider, token: str) -> None:
        return None


def test_pkce_state_ownership_replay_and_encrypted_token_custody(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = migrate(tmp_path / "oauth.db", monkeypatch)
    key = tmp_path / "test.key"
    key.write_bytes(base64.b64encode(os.urandom(32)))
    os.chmod(key, 0o600)
    store = SqlAlchemySecretPersistence(engine)
    keys = FileMasterKeyResolver(key, 1)
    manager = SecretManager(store, keys, lambda: NOW)
    resolver = SecretResolver(store, keys)
    transport = FakeTokens()
    provider = AccountOAuthProvider(
        "example",
        "https://provider.example/authorize",
        "https://provider.example/token",
        "public-client",
        "http://localhost:5173/api/v1/account-oauth/callback",
        ("read",),
    )
    service = AccountOAuthService(
        providers={provider.id: provider},
        persistence=SqlAlchemyAccountOAuthPersistence(engine),
        secrets_manager=manager,
        resolver=resolver,
        connections=AccountConnectionService(
            SqlAlchemyAccountConnectionPersistence(engine), lambda: NOW
        ),
        transport=transport,
        clock=lambda: NOW,
    )
    url = service.start(1, provider_id="example", scopes=("read",), display_name="Example")
    query = parse_qs(urlsplit(url).query)
    assert query["code_challenge_method"] == ["S256"]
    assert "code_verifier" not in query
    state = query["state"][0]
    with pytest.raises(AccountAuthorizationUnavailable):
        asyncio.run(service.finish(2, state=state, code="synthetic-code"))
    connection = asyncio.run(service.finish(1, state=state, code="synthetic-code"))
    assert connection.provider == "example"
    assert "synthetic-access-token" not in repr(connection)
    assert "synthetic-access-token" in resolver.resolve_active_value(1, connection.secret_id)
    with pytest.raises(AccountAuthorizationUnavailable):
        asyncio.run(service.finish(1, state=state, code="synthetic-code"))
    assert transport.calls == 1
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT ciphertext FROM secrets")).all()
        assert all(b"synthetic-access-token" not in bytes(row[0]) for row in rows)
        assert state not in str(conn.execute(text("SELECT * FROM account_oauth_requests")).all())
