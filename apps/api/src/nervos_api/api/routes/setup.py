"""One-time first-user setup endpoint."""

from fastapi import APIRouter, Response, status

from nervos_api.api.cookies import set_session_cookie
from nervos_api.api.dependencies import (
    AuthenticationServiceDependency,
    OriginDependency,
    SettingsDependency,
)
from nervos_api.api.schemas import CredentialRequest, UserResponse

router = APIRouter()


@router.post("/setup", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def setup(
    credentials: CredentialRequest,
    response: Response,
    origin: OriginDependency,
    service: AuthenticationServiceDependency,
    settings: SettingsDependency,
) -> UserResponse:
    """Create the sole initial administrator and log them in."""
    del origin
    issued = service.setup(credentials.username, credentials.password.get_secret_value())
    set_session_cookie(
        response,
        token=issued.token,
        expires_at=issued.expires_at,
        settings=settings,
    )
    return UserResponse.model_validate(issued.user, from_attributes=True)
