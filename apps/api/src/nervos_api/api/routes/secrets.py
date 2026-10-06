"""Stage-H1 Secret Manager control-plane routes (ADR 0032).

Every response here is :class:`SecretMetadata` and nothing else. There is no endpoint
that returns a stored value: a secret is write-only over HTTP, and the only read-back
path in the system is the Worker's brokered resolution inside an authorized dispatch.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Response
from nervos_core.application.secrets import SecretWrite
from nervos_core.domain.secrets import SecretKeyVersion, SecretMetadata
from pydantic import BaseModel, ConfigDict, Field

from nervos_api.api.dependencies import (
    CurrentUserDependency,
    OriginDependency,
    SecretManagerDependency,
)

router = APIRouter()


class SecretCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=8192)
    provider_hint: str | None = Field(default=None, max_length=64)


class SecretReplaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(min_length=1, max_length=8192)


class SecretStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(pattern=r"^(active|disabled|revoked)$")


class SecretResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    name: str
    provider_hint: str | None
    status: str
    key_version: int
    rotation_count: int
    created_at: datetime
    updated_at: datetime


class SecretKeyVersionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key_version: int
    active: bool
    created_at: datetime


def _view(secret: SecretMetadata) -> SecretResponse:
    return SecretResponse(
        id=secret.id,
        name=secret.name,
        provider_hint=secret.provider_hint,
        status=secret.status,
        key_version=secret.key_version,
        rotation_count=secret.rotation_count,
        created_at=secret.created_at,
        updated_at=secret.updated_at,
    )


def _key_view(version: SecretKeyVersion) -> SecretKeyVersionResponse:
    return SecretKeyVersionResponse(
        key_version=version.key_version,
        active=version.active,
        created_at=version.created_at,
    )


@router.get("/secrets", response_model=list[SecretResponse])
def list_secrets(
    user: CurrentUserDependency,
    manager: SecretManagerDependency,
) -> list[SecretResponse]:
    return [_view(secret) for secret in manager.list(user.id)]


@router.post("/secrets", response_model=SecretResponse, status_code=201)
def create_secret(
    body: SecretCreateRequest,
    origin: OriginDependency,
    user: CurrentUserDependency,
    manager: SecretManagerDependency,
) -> SecretResponse:
    del origin
    return _view(manager.create(user.id, SecretWrite(body.name, body.value, body.provider_hint)))


@router.get("/secrets/keys", response_model=list[SecretKeyVersionResponse])
def secret_key_versions(
    user: CurrentUserDependency,
    manager: SecretManagerDependency,
) -> list[SecretKeyVersionResponse]:
    """Report which master-key versions exist. Never key material."""
    del user
    return [_key_view(version) for version in manager.key_versions()]


@router.get("/secrets/{secret_id}", response_model=SecretResponse)
def inspect_secret(
    secret_id: int,
    user: CurrentUserDependency,
    manager: SecretManagerDependency,
) -> SecretResponse:
    return _view(manager.inspect(user.id, secret_id))


@router.put("/secrets/{secret_id}/value", response_model=SecretResponse)
def replace_secret(
    secret_id: int,
    body: SecretReplaceRequest,
    origin: OriginDependency,
    user: CurrentUserDependency,
    manager: SecretManagerDependency,
) -> SecretResponse:
    del origin
    return _view(manager.replace(user.id, secret_id, body.value))


@router.put("/secrets/{secret_id}/status", response_model=SecretResponse)
def set_secret_status(
    secret_id: int,
    body: SecretStatusRequest,
    origin: OriginDependency,
    user: CurrentUserDependency,
    manager: SecretManagerDependency,
) -> SecretResponse:
    del origin
    return _view(manager.set_status(user.id, secret_id, body.status))


@router.delete("/secrets/{secret_id}", status_code=204)
def delete_secret(
    secret_id: int,
    origin: OriginDependency,
    user: CurrentUserDependency,
    manager: SecretManagerDependency,
) -> Response:
    del origin
    manager.delete(user.id, secret_id)
    return Response(status_code=204)
