"""Pre-body and response security boundaries for the versioned API."""

from __future__ import annotations

from collections.abc import Sequence

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from nervos_api.api.errors import error_response
from nervos_api.config import Settings

_CREDENTIAL_PATHS = {"/api/v1/setup", "/api/v1/auth/login"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_MAX_CREDENTIAL_BODY_BYTES = 4096


class ApiSecurityHeadersMiddleware:
    """Attach baseline browser security headers to every API response."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope["path"]).startswith("/api/v1"):
            await self._app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                names = {name.lower() for name, _ in headers}
                if b"x-content-type-options" not in names:
                    headers.append((b"x-content-type-options", b"nosniff"))
                if b"referrer-policy" not in names:
                    headers.append((b"referrer-policy", b"no-referrer"))
                message["headers"] = headers
            await send(message)

        await self._app(scope, receive, send_with_headers)


class AuthenticationBoundaryMiddleware:
    """Reject unsafe API and credential requests before downstream work."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self._app = app
        self._origin = settings.app_origin.encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        path = str(scope["path"])
        method = str(scope["method"]).upper()
        raw_headers = list(scope["headers"])
        if path.startswith("/api/v1") and method not in _SAFE_METHODS:
            origins = self.header_values(raw_headers, b"origin")
            if origins != [self._origin]:
                await error_response(403, "invalid_origin", "Request origin is not allowed.")(
                    scope, receive, send
                )
                return

        if method == "POST" and path in _CREDENTIAL_PATHS:
            content_types = self.header_values(raw_headers, b"content-type")
            if len(content_types) != 1 or not self.is_json_content_type(content_types[0]):
                await error_response(
                    415, "unsupported_media_type", "Content-Type must be application/json."
                )(scope, receive, send)
                return

            lengths = self.header_values(raw_headers, b"content-length")
            if self.body_is_too_large(lengths):
                await error_response(413, "request_too_large", "Request body is too large.")(
                    scope, receive, send
                )
                return

            body = bytearray()
            more_body = True
            while more_body:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                if message["type"] != "http.request":
                    continue
                body.extend(message.get("body", b""))
                if len(body) > _MAX_CREDENTIAL_BODY_BYTES:
                    await error_response(413, "request_too_large", "Request body is too large.")(
                        scope, receive, send
                    )
                    return
                more_body = bool(message.get("more_body", False))

            delivered = False

            async def replay_body() -> Message:
                nonlocal delivered
                if delivered:
                    return {"type": "http.request", "body": b"", "more_body": False}
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            await self._app(scope, replay_body, send)
            return

        await self._app(scope, receive, send)

    @staticmethod
    def header_values(headers: Sequence[tuple[bytes, bytes]], name: bytes) -> list[bytes]:
        """Return all values for one case-insensitive ASGI header name."""
        return [value for key, value in headers if key.lower() == name]

    @staticmethod
    def is_json_content_type(value: bytes) -> bool:
        """Accept application/json with optional syntactically simple parameters."""
        try:
            media_type, *parameters = value.decode("ascii").split(";")
        except UnicodeDecodeError:
            return False
        if media_type.strip().lower() != "application/json":
            return False
        return all("=" in parameter and bool(parameter.strip()) for parameter in parameters)

    @staticmethod
    def body_is_too_large(content_lengths: Sequence[bytes] | bytes | None) -> bool:
        """Reject absent, duplicate, malformed, negative, or oversized lengths."""
        if isinstance(content_lengths, bytes):
            values = [content_lengths]
        elif content_lengths is None:
            values = []
        else:
            values = list(content_lengths)
        if len(values) != 1:
            return True
        try:
            length = int(values[0])
        except ValueError:
            return True
        return length < 0 or length > _MAX_CREDENTIAL_BODY_BYTES
