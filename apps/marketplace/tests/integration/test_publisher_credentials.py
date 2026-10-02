"""Real PostgreSQL credential broker with deterministic OIDC identity port."""

# pyright: basic

import asyncio
from urllib.parse import parse_qs, urlsplit

import pytest
from nervos_marketplace_service.application.authentication import Authentication
from nervos_marketplace_service.domain.errors import MarketplaceError
from nervos_marketplace_service.domain.identity import now, pkce
from nervos_marketplace_service.infrastructure.unit_of_work import PostgresUnitOfWork

pytestmark = pytest.mark.marketplace_integration


class Identity:
    async def authorization_url(self, state, nonce, verifier):
        self.state = state
        return "https://identity.example/authorize"

    async def redeem(self, code, verifier, nonce_hash):
        return {
            "issuer": "https://identity.example",
            "subject": "deterministic-owner",
            "authenticated_at": now(),
            "acr": "mfa",
            "amr": ["otp"],
        }


def test_browser_bound_consent_pkce_one_use_and_logout(database):
    identity = Identity()
    authentication = Authentication(PostgresUnitOfWork(database), identity, b"x" * 32)
    verifier, callback = "v" * 43, "http://127.0.0.1:12345/callback"
    _, browser = asyncio.run(
        authentication.start(
            "start",
            client_id="nervos-publisher-cli",
            callback=callback,
            cli_state="s" * 43,
            challenge=pkce(verifier),
        )
    )
    with pytest.raises(MarketplaceError):
        asyncio.run(
            authentication.callback(identity.state, "synthetic-code", "wrong-browser", "wrong")
        )
    session, csrf, cli = asyncio.run(
        authentication.callback(identity.state, "synthetic-code", browser, "callback")
    )
    assert cli
    actor = authentication.authenticate(session, True, "browser")
    with pytest.raises(MarketplaceError):
        authentication.csrf(actor, "wrong-csrf")
    authentication.csrf(actor, csrf)
    redirected = authentication.consent(actor, identity.state, browser, "consent")
    values = parse_qs(urlsplit(redirected).query)
    assert values["state"] == ["s" * 43]
    code = values["code"][0]
    with pytest.raises(MarketplaceError):
        authentication.exchange(
            code, "wrong-verifier" * 4, callback, "nervos-publisher-cli", "wrong-pkce"
        )
    token = authentication.exchange(code, verifier, callback, "nervos-publisher-cli", "exchange")[
        "access_token"
    ]
    with pytest.raises(MarketplaceError):
        authentication.exchange(code, verifier, callback, "nervos-publisher-cli", "replay")
    cli_actor = authentication.authenticate(token, False, "cli")
    authentication.logout(cli_actor, "logout")
    with pytest.raises(MarketplaceError):
        authentication.authenticate(token, False, "revoked")
