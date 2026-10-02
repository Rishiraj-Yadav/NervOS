"""Minimal hosted publisher API for the I2 MVP."""

from __future__ import annotations

import base64
from html import escape
from typing import Annotated, cast
from urllib.parse import parse_qs, urlencode
from uuid import UUID

from fastapi import APIRouter, Cookie, Header, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from nervos_marketplace_service.application.authentication import Authentication
from nervos_marketplace_service.application.publication import Publication
from nervos_marketplace_service.application.publication_ports import StaticPackageVerifier
from nervos_marketplace_service.application.publisher_management import PublisherManagement
from nervos_marketplace_service.domain.errors import MarketplaceError
from nervos_marketplace_service.domain.identity import Actor

router = APIRouter(prefix="/marketplace/v1", tags=["publisher"])


class PublisherCreateRequest(BaseModel):
    handle: str = Field(min_length=3, max_length=63)
    display_name: str = Field(min_length=1, max_length=256)
    kind: str = "individual"


class ProjectClaimRequest(BaseModel):
    publisher_id: UUID
    package_id: str = Field(min_length=3, max_length=128)


def _request_id(value: str | None) -> str:
    return value or "mvp-request"


def _services(request: Request) -> tuple[Authentication, PublisherManagement]:
    authentication = getattr(request.app.state, "publisher_authentication", None)
    publishers = getattr(request.app.state, "publisher_management", None)
    if authentication is None or publishers is None:
        raise MarketplaceError("service_unavailable", 503)
    return cast(Authentication, authentication), cast(PublisherManagement, publishers)


def _actor(
    request: Request, session: str | None, request_id: str | None, csrf: str | None = None
) -> tuple[Authentication, Actor]:
    authentication, _ = _services(request)
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        return authentication, authentication.authenticate(
            authorization[7:], False, _request_id(request_id)
        )
    if not session:
        raise MarketplaceError("authentication_required", 401)
    actor = authentication.authenticate(session, True, _request_id(request_id))
    if csrf is not None:
        authentication.csrf(actor, csrf)
    return authentication, actor


def _mutation_actor(
    request: Request, session: str | None, request_id: str | None, csrf: str | None
) -> tuple[Authentication, Actor]:
    if request.headers.get("Authorization", "").startswith("Bearer "):
        return _actor(request, session, request_id)
    if not csrf:
        raise MarketplaceError("csrf_invalid", 403)
    return _actor(request, session, request_id, csrf)


@router.post("/auth/logout")
def logout(
    request: Request,
    response: Response,
    session: Annotated[str | None, Cookie(alias="nervos_marketplace_session")] = None,
    csrf: Annotated[str | None, Header(alias="X-NervOS-CSRF")] = None,
    request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> dict[str, bool]:
    authentication, actor = _mutation_actor(request, session, request_id, csrf)
    authentication.logout(actor, _request_id(request_id))
    response.delete_cookie("nervos_marketplace_session")
    response.delete_cookie("nervos_marketplace_csrf")
    return {"logged_out": True}


@router.get("/auth/start", response_model=None)
async def auth_start(
    request: Request,
    response: Response,
    request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
    client_id: str | None = None,
    callback: str | None = None,
    cli_state: str | None = None,
    challenge: str | None = None,
    launch: bool = False,
) -> dict[str, str] | Response:
    authentication, _ = _services(request)
    url, browser = await authentication.start(
        _request_id(request_id),
        client_id=client_id,
        callback=callback,
        cli_state=cli_state,
        challenge=challenge,
    )
    if launch:
        response = RedirectResponse(url, status_code=303)
    response.set_cookie(
        "nervos_marketplace_browser", browser, httponly=True, samesite="lax", secure=True
    )
    return response if launch else {"authorization_url": url}


@router.get("/auth/callback", response_model=None)
async def auth_callback(
    request: Request,
    response: Response,
    state: str,
    code: str,
    browser: Annotated[str | None, Cookie(alias="nervos_marketplace_browser")] = None,
    request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> dict[str, str] | Response:
    if not browser:
        raise MarketplaceError("authentication_required", 401)
    authentication, _ = _services(request)
    session, csrf, cli = await authentication.callback(
        state, code, browser, _request_id(request_id)
    )
    if cli:
        response = RedirectResponse(
            "/marketplace/v1/auth/consent-page?" + urlencode({"state": state}), status_code=303
        )
    response.set_cookie(
        "nervos_marketplace_session", session, httponly=True, samesite="lax", secure=True
    )
    response.set_cookie(
        "nervos_marketplace_csrf", csrf, httponly=False, samesite="lax", secure=True
    )
    return response if cli else {"status": "authenticated"}


@router.post("/publishers")
def create_publisher(
    request: Request,
    payload: PublisherCreateRequest,
    session: Annotated[str | None, Cookie(alias="nervos_marketplace_session")] = None,
    csrf: Annotated[str | None, Header(alias="X-NervOS-CSRF")] = None,
    request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> dict[str, object]:
    _, actor = _mutation_actor(request, session, request_id, csrf)
    _, publishers = _services(request)
    return publishers.create(
        actor, payload.handle, payload.display_name, payload.kind, _request_id(request_id)
    )


@router.get("/publishers/me")
def my_publishers(
    request: Request,
    session: Annotated[str | None, Cookie(alias="nervos_marketplace_session")] = None,
    request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> list[dict[str, object]]:
    _, actor = _actor(request, session, request_id)
    _, publishers = _services(request)
    return publishers.mine(actor, _request_id(request_id))


@router.post("/projects")
def claim_project(
    request: Request,
    payload: ProjectClaimRequest,
    session: Annotated[str | None, Cookie(alias="nervos_marketplace_session")] = None,
    csrf: Annotated[str | None, Header(alias="X-NervOS-CSRF")] = None,
    request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> dict[str, object]:
    _, actor = _mutation_actor(request, session, request_id, csrf)
    _, publishers = _services(request)
    return publishers.claim(
        actor, payload.publisher_id, payload.package_id, _request_id(request_id)
    )


class TokenExchange(BaseModel):
    code: str = Field(min_length=1, max_length=256)
    verifier: str = Field(min_length=43, max_length=128)
    callback: str = Field(min_length=1, max_length=512)
    client_id: str = "nervos-publisher-cli"


@router.post("/auth/token")
def token_exchange(request: Request, payload: TokenExchange) -> dict[str, object]:
    authentication, _ = _services(request)
    return authentication.exchange(
        payload.code,
        payload.verifier,
        payload.callback,
        payload.client_id,
        _request_id(request.headers.get("X-Request-ID")),
    )


class Consent(BaseModel):
    state: str = Field(min_length=1, max_length=256)


@router.get("/auth/consent-page", response_class=HTMLResponse)
def consent_page(request: Request, state: str) -> HTMLResponse:
    Consent(state=state)
    _actor(request, request.cookies.get("nervos_marketplace_session"), None)
    csrf = request.cookies.get("nervos_marketplace_csrf", "")
    return HTMLResponse(
        '<!doctype html><html lang="en"><title>NervOS publisher authorization</title>'
        "<h1>Authorize the NervOS publisher CLI</h1>"
        "<p>This grants a short-lived publisher credential to the CLI that started this login.</p>"
        '<form method="post" action="/marketplace/v1/auth/consent-submit">'
        f'<input type="hidden" name="state" value="{escape(state, quote=True)}">'
        f'<input type="hidden" name="csrf" value="{escape(csrf, quote=True)}">'
        '<button type="submit">Authorize publisher CLI</button></form></html>',
        headers={
            "Content-Security-Policy": (
                "default-src 'none'; form-action 'self'; frame-ancestors 'none'"
            ),
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.post("/auth/consent-submit")
async def consent_submit(request: Request) -> RedirectResponse:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 1024:
            raise MarketplaceError("invalid_request")
        body.extend(chunk)
    try:
        form = parse_qs(body.decode("ascii"), strict_parsing=True, max_num_fields=2)
        state, csrf = form["state"], form["csrf"]
        if len(state) != 1 or len(csrf) != 1:
            raise ValueError
        payload = Consent(state=state[0])
    except (ValueError, KeyError):
        raise MarketplaceError("invalid_request") from None
    authentication, actor = _mutation_actor(
        request, request.cookies.get("nervos_marketplace_session"), None, csrf[0]
    )
    uri = authentication.consent(
        actor,
        payload.state,
        request.cookies.get("nervos_marketplace_browser", ""),
        _request_id(request.headers.get("X-Request-ID")),
    )
    return RedirectResponse(uri, status_code=303)


@router.post("/auth/consent")
def consent(request: Request, payload: Consent) -> dict[str, str]:
    authentication, actor = _mutation_actor(
        request,
        request.cookies.get("nervos_marketplace_session"),
        request.headers.get("X-Request-ID"),
        request.headers.get("X-NervOS-CSRF"),
    )
    return {
        "redirect_uri": authentication.consent(
            actor,
            payload.state,
            request.cookies.get("nervos_marketplace_browser", ""),
            _request_id(request.headers.get("X-Request-ID")),
        )
    }


def mutation_context(request: Request) -> tuple[Actor, PublisherManagement, str]:
    request_id = _request_id(request.headers.get("X-Request-ID"))
    _, actor = _mutation_actor(
        request,
        request.cookies.get("nervos_marketplace_session"),
        request_id,
        request.headers.get("X-NervOS-CSRF"),
    )
    _, publishers = _services(request)
    return actor, publishers, request_id


def publication(request: Request) -> Publication:
    result = getattr(request.app.state, "publication", None)
    if result is None:
        raise MarketplaceError("service_unavailable", 503)
    return cast(Publication, result)


class PublicKey(BaseModel):
    public_key: str = Field(min_length=44, max_length=44)


def decoded(value: str, length: int) -> bytes:
    try:
        result = base64.b64decode(value, validate=True)
        if len(result) != length:
            raise ValueError
        return result
    except ValueError:
        raise MarketplaceError("invalid_request") from None


@router.post("/publishers/{publisher_id}/keys/challenge")
def key_challenge(request: Request, publisher_id: UUID, payload: PublicKey) -> dict[str, object]:
    actor, publishers, request_id = mutation_context(request)
    return publishers.challenge(actor, publisher_id, decoded(payload.public_key, 32), request_id)


class KeyProof(BaseModel):
    challenge_id: UUID
    signature: str = Field(min_length=88, max_length=88)


@router.post("/publishers/{publisher_id}/keys/prove")
def key_proof(request: Request, publisher_id: UUID, payload: KeyProof) -> dict[str, object]:
    actor, publishers, request_id = mutation_context(request)
    return publishers.prove(
        actor, publisher_id, payload.challenge_id, decoded(payload.signature, 64), request_id
    )


class KeyAuthorization(BaseModel):
    key_id: UUID
    ownership_revision: int = Field(gt=0)


@router.post("/projects/{project_id}/keys")
def authorize_key(request: Request, project_id: UUID, payload: KeyAuthorization) -> dict[str, str]:
    actor, publishers, request_id = mutation_context(request)
    publishers.authorize_key(
        actor, project_id, payload.key_id, payload.ownership_revision, request_id
    )
    return {"status": "authorized"}


class UploadCreate(BaseModel):
    project_id: UUID
    archive_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0, le=256 * 1024**2)
    ownership_revision: int = Field(gt=0)
    idempotency_key: str = Field(pattern="^[A-Za-z0-9_-]{16,128}$")


@router.post("/uploads")
def create_upload(request: Request, payload: UploadCreate) -> dict[str, object]:
    actor, _, request_id = mutation_context(request)
    return publication(request).create_upload(
        actor,
        payload.project_id,
        payload.archive_sha256,
        payload.size_bytes,
        payload.ownership_revision,
        payload.idempotency_key,
        request_id,
    )


@router.put("/uploads/{operation_id}/artifact")
async def upload_artifact(request: Request, operation_id: UUID) -> dict[str, object]:
    actor, _, request_id = mutation_context(request)
    value = request.headers.get("Content-Length")
    if value is not None and not value.isdigit():
        raise MarketplaceError("invalid_request")
    return await publication(request).receive(
        actor,
        operation_id,
        request.stream(),
        int(value) if value else None,
        request_id,
    )


@router.get("/uploads/{operation_id}")
def upload_status(request: Request, operation_id: UUID) -> dict[str, object]:
    _, actor = _actor(
        request,
        request.cookies.get("nervos_marketplace_session"),
        request.headers.get("X-Request-ID"),
    )
    return publication(request).status(
        actor, operation_id, _request_id(request.headers.get("X-Request-ID"))
    )


@router.post("/uploads/{operation_id}/verify")
async def verify_upload(request: Request, operation_id: UUID) -> dict[str, object]:
    actor, _, request_id = mutation_context(request)
    verifier = cast(StaticPackageVerifier, request.app.state.static_verifier)
    return await publication(request).verify_upload(actor, operation_id, verifier, request_id)


class PublishRelease(BaseModel):
    exact_version: str = Field(min_length=1, max_length=64)
    release_id: UUID
    archive_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    content_digest: str = Field(pattern="^[0-9a-f]{64}$")
    ownership_revision: int = Field(gt=0)


@router.post("/projects/{project_id}/publish")
def publish_release(
    request: Request, project_id: UUID, payload: PublishRelease
) -> dict[str, object]:
    actor, _, request_id = mutation_context(request)
    return publication(request).publish(
        actor,
        project_id,
        payload.exact_version,
        payload.release_id,
        payload.archive_sha256,
        payload.content_digest,
        payload.ownership_revision,
        request_id,
    )


class DistributionUpdate(BaseModel):
    exact_version: str = Field(min_length=1, max_length=64)
    state: str = Field(pattern="^(available|yanked|revoked)$")
    status_revision: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=4096)


@router.post("/projects/{project_id}/distribution")
def distribution(request: Request, project_id: UUID, payload: DistributionUpdate) -> dict[str, str]:
    actor, _, request_id = mutation_context(request)
    publication(request).distribution(
        actor,
        project_id,
        payload.exact_version,
        payload.state,
        payload.status_revision,
        payload.reason,
        request_id,
    )
    return {"status": "updated"}


class ListingUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=256)
    summary: str = Field(max_length=4096)
    description: str = Field(max_length=32768)
    revision: int = Field(gt=0)


@router.post("/projects/{project_id}/listing")
def update_listing(request: Request, project_id: UUID, payload: ListingUpdate) -> dict[str, str]:
    actor, _, request_id = mutation_context(request)
    publication(request).listing(
        actor,
        project_id,
        payload.display_name,
        payload.summary,
        payload.description,
        payload.revision,
        request_id,
    )
    return {"status": "updated"}
