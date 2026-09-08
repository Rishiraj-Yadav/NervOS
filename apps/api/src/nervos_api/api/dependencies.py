"""FastAPI dependencies for authentication and request security."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, cast

from fastapi import Cookie, Depends, Request
from nervos_core.application.authentication import (
    AuthenticationRequired,
    AuthenticationService,
    PublicUser,
)

from nervos_api.api.cookies import SESSION_COOKIE_NAME
from nervos_api.api.errors import InvalidOrigin
from nervos_api.config import Settings


def utc_now() -> datetime:
    """Return the current aware UTC instant."""
    return datetime.now(UTC)


def get_settings(request: Request) -> Settings:
    """Return the settings composed into this FastAPI application."""
    return cast(Settings, request.app.state.settings)


def get_authentication_service(request: Request) -> AuthenticationService:
    """Return the concrete authentication service composed into the app."""
    return cast(AuthenticationService, request.app.state.authentication_service)


def require_configured_origin(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """Reject every absent or non-exact Origin before sensitive work."""
    if request.headers.get("origin") != settings.app_origin:
        raise InvalidOrigin


def get_current_user(
    service: Annotated[AuthenticationService, Depends(get_authentication_service)],
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
) -> PublicUser:
    """Resolve authenticated identity solely from the opaque session cookie."""
    if session_token is None:
        raise AuthenticationRequired
    return service.authenticate(session_token)


SettingsDependency = Annotated[Settings, Depends(get_settings)]
AuthenticationServiceDependency = Annotated[
    AuthenticationService,
    Depends(get_authentication_service),
]
OriginDependency = Annotated[None, Depends(require_configured_origin)]
CurrentUserDependency = Annotated[PublicUser, Depends(get_current_user)]
