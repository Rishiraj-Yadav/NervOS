"""Bounded local client for the hosted Marketplace catalog and exact archives.

The local API is the only caller exposed to the dashboard.  This MVP client sends
public identifiers only; installation authority remains with Stage G.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast
from urllib.parse import quote, urlsplit

import httpx
from nervos_api.application.marketplace_transport import marketplace_transport
from nervos_core.application.errors import PersistenceUnavailable


class MarketplaceDiscoveryService:
    def __init__(
        self,
        origin: str | None,
        timeout_seconds: float = 10.0,
        *,
        development: bool = False,
        allow_private: bool = False,
    ) -> None:
        self.origin = self._origin(origin) if origin else None
        self.timeout = httpx.Timeout(timeout_seconds)
        self.development, self.allow_private = development, allow_private
        if self.origin and urlsplit(self.origin).scheme == "http" and not development:
            raise ValueError("Loopback HTTP Marketplace requires development/test mode")

    def _transport(self) -> httpx.AsyncHTTPTransport:
        return marketplace_transport(
            urlsplit(self.origin or "").hostname or "", self.allow_private, self.development
        )

    @staticmethod
    def _origin(value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or (
                parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            )
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("marketplace origin must be an HTTP(S) origin")
        return value.rstrip("/")

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        if self.origin is None:
            raise PersistenceUnavailable
        try:
            async with (
                asyncio.timeout(self.timeout.read or 10),
                httpx.AsyncClient(
                    base_url=self.origin,
                    timeout=self.timeout,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self._transport(),
                ) as client,
            ):
                async with client.stream("GET", path, params=params) as response:
                    if response.status_code != 200:
                        raise PersistenceUnavailable
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > 1024 * 1024:
                            raise PersistenceUnavailable
                        body.extend(chunk)
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise PersistenceUnavailable
                return cast(dict[str, Any], payload)
        except (httpx.HTTPError, ValueError, TimeoutError):
            raise PersistenceUnavailable from None

    async def search(self, query: str, limit: int, cursor: str | None) -> dict[str, Any]:
        params = {"q": query, "limit": str(limit)}
        if cursor:
            params["cursor"] = cursor
        return await self._get("/marketplace/v1/packages", params)

    async def package(self, package_id: str) -> dict[str, Any]:
        return await self._get(
            "/marketplace/v1/packages/" + quote(package_id, safe=""),
            {},
        )

    async def release(self, package_id: str, version: str) -> dict[str, Any]:
        return await self._get(
            "/marketplace/v1/packages/"
            + quote(package_id, safe="")
            + "/versions/"
            + quote(version, safe=""),
            {},
        )

    async def versions(self, package_id: str, cursor: str | None = None) -> dict[str, Any]:
        params = {"limit": "100", "include_unavailable": "true"}
        if cursor:
            params["cursor"] = cursor
        return await self._get(
            "/marketplace/v1/packages/" + quote(package_id, safe="") + "/versions", params
        )

    async def download(
        self,
        package_id: str,
        version: str,
        max_bytes: int = 256 * 1024 * 1024,
        acknowledgement: str | None = None,
    ) -> bytes:
        if self.origin is None:
            raise PersistenceUnavailable
        try:
            async with (
                asyncio.timeout(120),
                httpx.AsyncClient(
                    base_url=self.origin,
                    timeout=self.timeout,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self._transport(),
                ) as client,
            ):
                request = client.build_request(
                    "GET",
                    "/marketplace/v1/packages/"
                    + quote(package_id, safe="")
                    + "/versions/"
                    + quote(version, safe="")
                    + "/artifact",
                )
                headers = (
                    {"X-NervOS-Acknowledge-Yanked": acknowledgement} if acknowledgement else {}
                )
                async with client.stream(request.method, request.url, headers=headers) as response:
                    if response.status_code != 200:
                        raise PersistenceUnavailable
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise PersistenceUnavailable
                    return bytes(body)
        except (httpx.HTTPError, ValueError, TimeoutError):
            raise PersistenceUnavailable from None
