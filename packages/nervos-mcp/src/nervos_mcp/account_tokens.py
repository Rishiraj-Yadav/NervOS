"""Bounded OAuth token transport; endpoint config is operator owned, never agent input."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Mapping
from typing import cast

import httpx2
from nervos_core.application.account_connections import validate_scopes
from nervos_core.application.account_oauth import (
    AccountAuthorizationUnavailable,
    AccountOAuthProvider,
    AccountTokens,
)
from pydantic import ConfigDict, TypeAdapter

_PROVIDERS = TypeAdapter(list[dict[str, object]], config=ConfigDict(strict=True))


def load_account_providers(raw: str) -> dict[str, AccountOAuthProvider]:
    if not raw.strip():
        return {}
    try:
        rows = _PROVIDERS.validate_json(raw)
        result: dict[str, AccountOAuthProvider] = {}
        allowed = {
            "id",
            "authorization_endpoint",
            "token_endpoint",
            "client_id",
            "redirect_uri",
            "scopes",
            "revocation_endpoint",
            "client_secret_env",
        }
        for row in rows:
            if set(row) - allowed:
                raise ValueError
            provider = TypeAdapter(AccountOAuthProvider).validate_python(row)
            if (
                provider.id in result
                or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", provider.id) is None
                or (
                    provider.client_secret_env is not None
                    and re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", provider.client_secret_env) is None
                )
            ):
                raise ValueError
            result[provider.id] = provider
        return result
    except Exception:
        raise AccountAuthorizationUnavailable from None


class HttpAccountTokenTransport:
    async def _post(
        self, endpoint: str, provider: AccountOAuthProvider, form: Mapping[str, str]
    ) -> bytes:
        data = dict(form)
        if provider.client_secret_env:
            value = os.environ.get(provider.client_secret_env)
            if not value:
                raise AccountAuthorizationUnavailable
            data["client_secret"] = value
        try:
            async with asyncio.timeout(15):
                async with httpx2.AsyncClient(
                    timeout=10, follow_redirects=False, trust_env=False
                ) as client:
                    async with client.stream("POST", endpoint, data=data) as response:
                        if not 200 <= response.status_code < 300:
                            raise AccountAuthorizationUnavailable
                        payload = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(payload) + len(chunk) > 65536:
                                raise AccountAuthorizationUnavailable
                            payload.extend(chunk)
                        return bytes(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise AccountAuthorizationUnavailable from None

    async def exchange(
        self, provider: AccountOAuthProvider, form: Mapping[str, str]
    ) -> AccountTokens:
        payload = await self._post(provider.token_endpoint, provider, form)
        try:
            row = cast(dict[str, object], json.loads(payload))
            access = row["access_token"]
            refresh = row.get("refresh_token")
            expiry = row.get("expires_in")
            scope = row.get("scope", "")
            if (
                str(row.get("token_type", "")).lower() != "bearer"
                or not isinstance(access, str)
                or not 1 <= len(access) <= 3072
                or any(ord(ch) <= 32 or ord(ch) >= 127 for ch in access)
                or (
                    refresh is not None
                    and (not isinstance(refresh, str) or not 1 <= len(refresh) <= 3072)
                )
                or (expiry is not None and (type(expiry) is not int or not 1 <= expiry <= 31536000))
                or not isinstance(scope, str)
            ):
                raise ValueError
            scopes = validate_scopes(tuple(scope.split())) if scope else ()
            return AccountTokens(access, refresh, expiry, scopes)
        except Exception:
            raise AccountAuthorizationUnavailable from None

    async def revoke(self, provider: AccountOAuthProvider, token: str) -> None:
        if provider.revocation_endpoint is not None:
            await self._post(
                provider.revocation_endpoint,
                provider,
                {"token": token, "client_id": provider.client_id},
            )
