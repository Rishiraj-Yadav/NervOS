"""Pre-body security boundary for authentication mutations."""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send

from nervos_api.api.errors import error_response
from nervos_api.config import Settings

_CREDENTIAL_PATHS = {"/api/v1/setup", "/api/v1/auth/login"}
_AUTH_MUTATION_PATHS = _CREDENTIAL_PATHS | {"/api/v1/auth/logout"}
_MAX_CREDENTIAL_BODY_BYTES = 4096


class AuthenticationBoundaryMiddleware:
    """Reject unsafe authentication requests before buffering their bodies."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self._app = app
        self._origin = settings.app_origin.encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        path = str(scope["path"])
        method = str(scope["method"])
        if method != "POST" or path not in _AUTH_MUTATION_PATHS:
            await self._app(scope, receive, send)
            return

        headers = dict(scope["headers"])
        if headers.get(b"origin") != self._origin:
            await error_response(403, "invalid_origin", "Request origin is not allowed.")(
                scope, receive, send
            )
            return

        if path in _CREDENTIAL_PATHS and self.body_is_too_large(headers.get(b"content-length")):
            await error_response(413, "request_too_large", "Request body is too large.")(
                scope, receive, send
            )
            return

        await self._app(scope, receive, send)

    @staticmethod
    def body_is_too_large(content_length: bytes | None) -> bool:
        if content_length is None:
            return True
        try:
            return int(content_length) > _MAX_CREDENTIAL_BODY_BYTES
        except ValueError:
            return True
