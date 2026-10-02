"""Provider-neutral OIDC client. Discovery failures never affect catalog readiness."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Protocol, cast
from urllib.parse import urlsplit

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from joserfc import jwk, jwt

from nervos_marketplace_service.config import MarketplaceSettings
from nervos_marketplace_service.domain.errors import MarketplaceError
from nervos_marketplace_service.domain.identity import Record, now, token_hash


class OAuthClient(Protocol):
    """The Authlib methods used here; its httpx compatibility alias is untyped."""

    def create_authorization_url(self, url: str, **kwargs: object) -> tuple[str, str]: ...
    async def fetch_token(self, url: str, **kwargs: object) -> Record: ...
    async def aclose(self) -> None: ...


class BoundedOIDCStream(httpx.AsyncByteStream):
    def __init__(self, source: httpx.AsyncByteStream) -> None:
        self.source = source

    async def __aiter__(self) -> AsyncIterator[bytes]:
        size = 0
        async for block in self.source:
            size += len(block)
            if size > 256 * 1024:
                raise httpx.StreamError("OIDC response exceeds limit")
            yield block

    async def aclose(self) -> None:
        await self.source.aclose()


class BoundedOIDCTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.transport = httpx.AsyncHTTPTransport(trust_env=False)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.transport.handle_async_request(request)
        assert isinstance(response.stream, httpx.AsyncByteStream)
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=BoundedOIDCStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self.transport.aclose()


class OIDCClient:
    def __init__(self, settings: MarketplaceSettings) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False)

    async def _json(self, url: str) -> Record:
        try:
            async with asyncio.timeout(10), self.client.stream("GET", url) as response:
                if response.status_code != 200 or "application/json" not in response.headers.get(
                    "content-type", ""
                ):
                    raise ValueError
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 256 * 1024:
                        raise ValueError
            import json

            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError
            return cast(Record, value)
        except Exception:
            raise MarketplaceError("service_unavailable", 503) from None

    def _endpoint(self, url: object) -> str:
        if not isinstance(url, str) or len(url) > 2048:
            raise MarketplaceError("service_unavailable", 503)
        selected, issuer = urlsplit(url), urlsplit(self.settings.oidc_issuer or "")
        if (selected.scheme, selected.hostname, selected.port) != (
            issuer.scheme,
            issuer.hostname,
            issuer.port,
        ) or (selected.username or selected.password or selected.fragment):
            raise MarketplaceError("service_unavailable", 503)
        return url

    async def discovery(self) -> Record:
        value = await self._json(
            (self.settings.oidc_issuer or "").rstrip("/") + "/.well-known/openid-configuration"
        )
        if value.get("issuer") != self.settings.oidc_issuer:
            raise MarketplaceError("service_unavailable", 503)
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            value[key] = self._endpoint(value.get(key))
        return value

    def oauth(self) -> OAuthClient:
        return cast(
            OAuthClient,
            AsyncOAuth2Client(
                client_id=self.settings.oidc_client_id,
                client_secret=self.settings.oidc_client_secret.get_secret_value()
                if self.settings.oidc_client_secret
                else None,
                redirect_uri=(self.settings.public_origin or "").rstrip("/")
                + "/marketplace/v1/auth/callback",
                scope="openid",
                code_challenge_method="S256",
                timeout=5,
                trust_env=False,
                follow_redirects=False,
                transport=BoundedOIDCTransport(),
                headers={"Accept-Encoding": "identity"},
            ),
        )

    async def authorization_url(self, state: str, nonce: str, verifier: str) -> str:
        metadata = await self.discovery()
        client = self.oauth()
        try:
            url, _ = client.create_authorization_url(
                metadata["authorization_endpoint"],
                state=state,
                nonce=nonce,
                code_verifier=verifier,
                max_age=self.settings.recent_auth_max_age_seconds,
            )
            return str(url)
        finally:
            await client.aclose()

    async def redeem(self, code: str, verifier: str, nonce_hash: str) -> Record:
        try:
            metadata = await self.discovery()
            client = self.oauth()
            try:
                async with asyncio.timeout(10):
                    token = await client.fetch_token(
                        metadata["token_endpoint"],
                        code=code,
                        code_verifier=verifier,
                        grant_type="authorization_code",
                    )
            finally:
                await client.aclose()
            encoded = token.get("id_token")
            if not isinstance(encoded, str) or len(encoded) > 32768:
                raise ValueError
            key_data = await self._json(metadata["jwks_uri"])
            key_set = jwk.KeySet.import_key_set(cast(jwk.KeySetSerialization, key_data))
            decoded = jwt.decode(encoded, key_set, algorithms=self.settings.oidc_algorithms)
            if "jku" in decoded.header or "x5u" in decoded.header:
                raise ValueError
            claims = decoded.claims
            issuer, client_id = self.settings.oidc_issuer, self.settings.oidc_client_id
            if issuer is None or client_id is None:
                raise ValueError
            jwt.JWTClaimsRegistry(
                iss={"essential": True, "value": issuer},
                aud={"essential": True, "value": client_id},
                exp={"essential": True},
                iat={"essential": True},
                sub={"essential": True},
                nonce={"essential": True},
                auth_time={"essential": True},
            ).validate(claims)
            audience = claims["aud"]
            if (
                isinstance(audience, list)
                and len(cast(list[object], audience)) > 1
                and claims.get("azp") != self.settings.oidc_client_id
            ) or ("azp" in claims and claims["azp"] != self.settings.oidc_client_id):
                raise ValueError
            if token_hash(claims["nonce"]) != nonce_hash:
                raise ValueError
            for name in ("exp", "iat", "auth_time", "nbf"):
                if name in claims and (
                    not isinstance(claims[name], int) or isinstance(claims[name], bool)
                ):
                    raise ValueError
            authenticated = datetime.fromtimestamp(claims["auth_time"], UTC)
            if (
                claims["iat"] > now().timestamp() + 30
                or not 0
                <= (now() - authenticated).total_seconds()
                <= self.settings.recent_auth_max_age_seconds
            ):
                raise ValueError
            if not isinstance(claims["sub"], str) or not 1 <= len(claims["sub"].encode()) <= 512:
                raise ValueError
            amr = claims.get("amr", [])
            if (
                not isinstance(amr, list)
                or len(cast(list[object], amr)) > 20
                or any(
                    not isinstance(item, str) or len(item) > 64 for item in cast(list[object], amr)
                )
            ):
                raise ValueError
            acr = claims.get("acr")
            if acr is not None and (not isinstance(acr, str) or len(acr) > 256):
                raise ValueError
            return {
                "issuer": claims["iss"],
                "subject": claims["sub"],
                "authenticated_at": authenticated,
                "acr": acr,
                "amr": amr,
            }
        except MarketplaceError:
            raise
        except Exception:
            raise MarketplaceError("authentication_required", 401) from None

    async def close(self) -> None:
        await self.client.aclose()
