"""HTTP cookie policy for opaque authentication sessions."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit

from fastapi import Response
from nervos_core.application.authentication import SESSION_LIFETIME_SECONDS

from nervos_api.config import Settings

SESSION_COOKIE_NAME = "nervos_session"
SESSION_COOKIE_PATH = "/"


def cookie_is_secure(settings: Settings) -> bool:
    """Derive Secure only from trusted validated configuration."""
    return settings.environment == "production" or urlsplit(settings.app_origin).scheme == "https"


def set_session_cookie(
    response: Response,
    *,
    token: str,
    expires_at: datetime,
    settings: Settings,
) -> None:
    """Set the host-only opaque-session cookie."""
    max_age = SESSION_LIFETIME_SECONDS
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=max_age,
        expires=expires_at,
        path=SESSION_COOKIE_PATH,
        secure=cookie_is_secure(settings),
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"


def clear_session_cookie(response: Response, settings: Settings) -> None:
    """Clear the cookie with the same scope and security attributes."""
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path=SESSION_COOKIE_PATH,
        secure=cookie_is_secure(settings),
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
