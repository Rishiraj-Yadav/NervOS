"""Stage-H2/H3/H5 control-plane routes: connections, approvals, and publisher trust.

Each resource is owner-scoped and server-authorized: the routes pass the authenticated
user id down and never accept one from the request body, which is the rule every
owner-scoped surface in this API already follows.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Query, Response
from fastapi.responses import RedirectResponse
from nervos_core.application.account_connections import AccountConnection
from nervos_core.application.approvals import ActionApproval
from pydantic import BaseModel, ConfigDict, Field

from nervos_api.api.dependencies import (
    AccountConnectionServiceDependency,
    AccountOAuthDependency,
    ApprovalServiceDependency,
    CurrentUserDependency,
    OriginDependency,
    PublisherTrustServiceDependency,
    SettingsDependency,
)

router = APIRouter()


class AccountOAuthStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    scopes: list[str] = Field(min_length=1, max_length=32)


class AccountProviderResponse(BaseModel):
    id: str
    scopes: list[str]


class AccountAuthorizationResponse(BaseModel):
    authorization_url: str


@router.get("/account-oauth/providers", response_model=list[AccountProviderResponse])
def account_providers(
    user: CurrentUserDependency, service: AccountOAuthDependency
) -> list[AccountProviderResponse]:
    del user
    return [AccountProviderResponse(id=p.id, scopes=list(p.scopes)) for p in service.providers()]


@router.post("/account-oauth/start", response_model=AccountAuthorizationResponse)
def start_account_authorization(
    body: AccountOAuthStartRequest,
    user: CurrentUserDependency,
    origin: OriginDependency,
    service: AccountOAuthDependency,
) -> AccountAuthorizationResponse:
    del origin
    url = service.start(
        user.id,
        provider_id=body.provider,
        scopes=tuple(body.scopes),
        display_name=body.display_name,
    )
    return AccountAuthorizationResponse(authorization_url=url)


@router.get("/account-oauth/callback")
async def complete_account_authorization(
    user: CurrentUserDependency,
    service: AccountOAuthDependency,
    settings: SettingsDependency,
    state: str = Query(min_length=32, max_length=128),
    code: str = Query(min_length=1, max_length=4096),
) -> RedirectResponse:
    await service.finish(user.id, state=state, code=code)
    return RedirectResponse(
        settings.app_origin + "/security",
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


# ---------------------------------------------------------------------------------------
# H2: account connections (ADR 0033). No response field carries a credential.
# ---------------------------------------------------------------------------------------


class AccountConnectionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    display_name: str = Field(min_length=1, max_length=128)
    secret_id: int = Field(gt=0)
    scopes: list[str] = Field(min_length=1, max_length=32)
    expires_at: datetime | None = None


class AccountConnectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    provider: str
    display_name: str
    secret_id: int
    state: str
    scopes: list[str]
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
    revocation_outcome: str | None = None


def _connection_view(connection: AccountConnection) -> AccountConnectionResponse:
    return AccountConnectionResponse(
        id=connection.id,
        provider=connection.provider,
        display_name=connection.display_name,
        secret_id=connection.secret_id,
        state=connection.state,
        scopes=list(connection.scopes),
        expires_at=connection.expires_at,
        created_at=connection.created_at,
        updated_at=connection.updated_at,
        revocation_outcome=connection.revocation_outcome,
    )


@router.get("/account-connections", response_model=list[AccountConnectionResponse])
def list_account_connections(
    user: CurrentUserDependency,
    service: AccountConnectionServiceDependency,
) -> list[AccountConnectionResponse]:
    return [_connection_view(item) for item in service.list(user.id)]


@router.post("/account-connections", response_model=AccountConnectionResponse, status_code=201)
def create_account_connection(
    body: AccountConnectionCreateRequest,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: AccountConnectionServiceDependency,
) -> AccountConnectionResponse:
    del origin
    return _connection_view(
        service.connect(
            user.id,
            provider=body.provider,
            display_name=body.display_name,
            secret_id=body.secret_id,
            scopes=tuple(body.scopes),
            expires_at=body.expires_at,
        )
    )


@router.get("/account-connections/{connection_id}", response_model=AccountConnectionResponse)
def get_account_connection(
    connection_id: int,
    user: CurrentUserDependency,
    service: AccountConnectionServiceDependency,
) -> AccountConnectionResponse:
    return _connection_view(service.get(user.id, connection_id))


@router.post("/account-connections/{connection_id}/disconnect", status_code=204)
async def disconnect_account_connection(
    connection_id: int,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: AccountOAuthDependency,
) -> Response:
    del origin
    await service.disconnect(user.id, connection_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------------------
# H3: per-action approvals (ADR 0034). The preview is the only rendering path, and it is
# already redacted and bounded when the row is written.
# ---------------------------------------------------------------------------------------


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: bool


class ApprovalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    agent_instance_id: int
    run_id: int
    tool_sequence: int
    upstream_name: str
    fingerprint: str
    input_digest: str
    preview: dict[str, object]
    state: str
    requested_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    consumed_at: datetime | None


def _approval_view(approval: ActionApproval) -> ApprovalResponse:
    return ApprovalResponse(
        id=approval.id,
        agent_instance_id=approval.agent_instance_id,
        run_id=approval.run_id,
        tool_sequence=approval.tool_sequence,
        upstream_name=approval.upstream_name,
        fingerprint=approval.fingerprint,
        input_digest=approval.input_digest,
        preview={key: value for key, value in approval.preview.items()},
        state=approval.state,
        requested_at=approval.requested_at,
        expires_at=approval.expires_at,
        decided_at=approval.decided_at,
        consumed_at=approval.consumed_at,
    )


@router.get("/action-approvals", response_model=list[ApprovalResponse])
def list_pending_approvals(
    user: CurrentUserDependency,
    service: ApprovalServiceDependency,
) -> list[ApprovalResponse]:
    return [_approval_view(item) for item in service.list_pending(owner_user_id=user.id)]


@router.post("/action-approvals/{approval_id}/decision", response_model=ApprovalResponse)
def decide_approval(
    approval_id: int,
    body: ApprovalDecisionRequest,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: ApprovalServiceDependency,
) -> ApprovalResponse:
    del origin
    return _approval_view(
        service.decide(owner_user_id=user.id, approval_id=approval_id, approve=body.approve)
    )


@router.post("/action-approvals/{approval_id}/cancel", response_model=ApprovalResponse)
def cancel_approval(
    approval_id: int,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: ApprovalServiceDependency,
) -> ApprovalResponse:
    del origin
    return _approval_view(service.cancel(owner_user_id=user.id, approval_id=approval_id))


# ---------------------------------------------------------------------------------------
# H5: publisher trust and local revocation (ADR 0036).
# ---------------------------------------------------------------------------------------


class PublisherTrustDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: str = Field(pattern=r"^(trusted|untrusted|revoked)$")
    reason: str | None = Field(default=None, max_length=512)


class PublisherTrustResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signer_fingerprint: str
    state: str
    reason: str | None
    source: str
    created_at: datetime
    updated_at: datetime


@router.get("/publisher-trust", response_model=list[PublisherTrustResponse])
def list_publisher_trust(
    user: CurrentUserDependency,
    service: PublisherTrustServiceDependency,
) -> list[PublisherTrustResponse]:
    del user
    return [
        PublisherTrustResponse(
            signer_fingerprint=record.signer_fingerprint,
            state=record.state,
            reason=record.reason,
            source=record.source,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
        for record in service.all()
    ]


@router.put("/publisher-trust/{signer_fingerprint}", response_model=PublisherTrustResponse)
def set_publisher_trust(
    signer_fingerprint: str,
    body: PublisherTrustDecisionRequest,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: PublisherTrustServiceDependency,
) -> PublisherTrustResponse:
    del origin
    record = service.decide(
        signer_fingerprint=signer_fingerprint,
        state=body.state,
        decided_by=user.id,
        reason=body.reason,
    )
    return PublisherTrustResponse(
        signer_fingerprint=record.signer_fingerprint,
        state=record.state,
        reason=record.reason,
        source=record.source,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )
