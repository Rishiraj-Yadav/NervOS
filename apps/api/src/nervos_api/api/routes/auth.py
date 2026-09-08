"""Local authentication session endpoints."""

from typing import Annotated

from fastapi import APIRouter, Cookie, Response, status

from nervos_api.api.cookies import SESSION_COOKIE_NAME, clear_session_cookie, set_session_cookie
from nervos_api.api.dependencies import (
    AuthenticationServiceDependency,
    CurrentUserDependency,
    OriginDependency,
    SettingsDependency,
)
from nervos_api.api.schemas import CredentialRequest, UserResponse

router = APIRouter(prefix="/auth")


@router.post("/login", response_model=UserResponse)
def login(
    credentials: CredentialRequest,
    response: Response,
    origin: OriginDependency,
    service: AuthenticationServiceDependency,
    settings: SettingsDependency,
) -> UserResponse:
    """Authenticate credentials and issue a fresh opaque session."""
    del origin
    issued = service.login(credentials.username, credentials.password.get_secret_value())
    set_session_cookie(
        response,
        token=issued.token,
        expires_at=issued.expires_at,
        settings=settings,
    )
    return UserResponse.model_validate(issued.user, from_attributes=True)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    origin: OriginDependency,
    service: AuthenticationServiceDependency,
    settings: SettingsDependency,
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
) -> None:
    """Idempotently revoke and clear the current browser session."""
    del origin
    service.logout(session_token)
    clear_session_cookie(response, settings)


@router.get("/me", response_model=UserResponse)
def me(user: CurrentUserDependency, response: Response) -> UserResponse:
    """Return safe data for the authenticated user."""
    response.headers["Cache-Control"] = "no-store"
    return UserResponse.model_validate(user, from_attributes=True)
