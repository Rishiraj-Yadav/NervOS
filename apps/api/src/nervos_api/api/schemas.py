"""Pydantic schemas exposed by the Stage A API."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class HealthResponse(BaseModel):
    """Public liveness response."""

    status: Literal["ok"] = "ok"


class CredentialRequest(BaseModel):
    """Credential input whose representation never exposes its password."""

    model_config = ConfigDict(extra="forbid")

    username: Annotated[str, Field(min_length=1, max_length=128)]
    password: Annotated[SecretStr, Field(min_length=12, max_length=128)]


class SetupStatusResponse(BaseModel):
    """Public first-run setup availability state."""

    setup_complete: bool


class UserResponse(BaseModel):
    """Safe authenticated-user response."""

    id: int
    username: str
    role: str
    is_active: bool


class ErrorDetail(BaseModel):
    """Stable public error information."""

    code: str
    message: str


class ErrorResponse(BaseModel):
    """Stable error envelope."""

    error: ErrorDetail
