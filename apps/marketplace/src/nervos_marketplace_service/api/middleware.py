"""ASGI correlation and allowlisted logs; no raw request URL/header logging."""

import logging
import time
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("nervos.marketplace")


class PublicReadMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.monotonic()

        async def safe_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                for key, value in (
                    (b"x-request-id", request_id.encode()),
                    (b"x-content-type-options", b"nosniff"),
                ):
                    if not any(existing.lower() == key for existing, _ in headers):
                        headers.append((key, value))
                if not any(key.lower() == b"cache-control" for key, _ in headers):
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
                route = scope.get("route")
                logger.info(
                    "request",
                    extra={
                        "request_id": request_id,
                        "route": getattr(route, "path", "unmatched"),
                        "status": message["status"],
                        "duration": time.monotonic() - started,
                    },
                )
            await send(message)

        await self.app(scope, receive, safe_send)
