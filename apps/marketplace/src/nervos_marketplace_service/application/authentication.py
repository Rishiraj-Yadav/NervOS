"""Hosted opaque credentials and a browser-bound, single-use CLI code broker."""

from __future__ import annotations

import hmac
import re
import secrets
from datetime import timedelta
from typing import Protocol
from urllib.parse import urlencode, urlsplit
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from nervos_marketplace_service.application.publication_ports import PublicationUnitOfWork
from nervos_marketplace_service.domain.errors import MarketplaceError
from nervos_marketplace_service.domain.identity import Actor, Record, now, pkce, token_hash


class OIDC(Protocol):
    async def authorization_url(self, state: str, nonce: str, verifier: str) -> str: ...
    async def redeem(self, code: str, verifier: str, nonce_hash: str) -> Record: ...


def redirect_uri(value: str) -> str:
    try:
        url = urlsplit(value)
        if (
            url.scheme != "http"
            or url.hostname != "127.0.0.1"
            or url.path != "/callback"
            or (not url.port or url.username or url.password or url.query or url.fragment)
            or len(value) > 256
        ):
            raise ValueError
    except ValueError:
        raise MarketplaceError("invalid_request") from None
    return value


class Authentication:
    def __init__(
        self,
        uow: PublicationUnitOfWork,
        oidc: OIDC,
        encryption_key: bytes,
        session_seconds: int = 28800,
        idle_seconds: int = 1800,
        token_seconds: int = 900,
    ) -> None:
        self.uow, self.oidc = uow, oidc
        self.cipher = AESGCM(encryption_key)
        self.session_seconds, self.idle_seconds, self.token_seconds = (
            session_seconds,
            idle_seconds,
            token_seconds,
        )

    async def start(
        self,
        request_id: str,
        *,
        client_id: str | None = None,
        callback: str | None = None,
        cli_state: str | None = None,
        challenge: str | None = None,
        scope: str = "publisher",
        publisher_id: UUID | None = None,
    ) -> tuple[str, str]:
        if scope not in {"publisher", "operator"}:
            raise MarketplaceError("invalid_request")
        if callback is not None:
            redirect_uri(callback)
            if (
                client_id != "nervos-publisher-cli"
                or not cli_state
                or not challenge
                or (
                    re.fullmatch(r"[A-Za-z0-9_-]{20,256}", cli_state) is None
                    or re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge) is None
                )
            ):
                raise MarketplaceError("invalid_request")
        state, browser, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(4))
        selected = await self.oidc.authorization_url(state, nonce, verifier)
        iv = secrets.token_bytes(12)
        encrypted = iv + self.cipher.encrypt(iv, verifier.encode(), state.encode())
        with self.uow.transaction(request_id) as tx:
            tx.insert(
                "auth_transactions",
                {
                    "state_hash": token_hash(state),
                    "browser_hash": token_hash(browser),
                    "nonce_hash": token_hash(nonce),
                    "encrypted_verifier": encrypted,
                    "redirect_uri": callback,
                    "cli_state": cli_state,
                    "cli_challenge": challenge,
                    "scope": scope,
                    "publisher_id": publisher_id,
                    "expires_at": now() + timedelta(minutes=5),
                    "consumed": False,
                    "consented": False,
                },
            )
        return selected, browser

    async def callback(
        self, state: str, code: str, browser: str, request_id: str
    ) -> tuple[str, str, bool]:
        with self.uow.transaction(request_id) as tx:
            value = tx.get("auth_transactions", {"state_hash": token_hash(state)})
            if (
                value["consumed"]
                or value["expires_at"] <= now()
                or not hmac.compare_digest(value["browser_hash"], token_hash(browser))
            ):
                raise MarketplaceError("authentication_required", 401)
            encrypted = bytes(value["encrypted_verifier"])
            verifier = self.cipher.decrypt(encrypted[:12], encrypted[12:], state.encode()).decode()
            # Consume before network exchange so failed redemptions cannot replay it.
            tx.update(
                "auth_transactions",
                {"state_hash": token_hash(state)},
                {"consumed": True, "encrypted_verifier": b""},
            )
        identity = await self.oidc.redeem(code, verifier, value["nonce_hash"])
        session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.uow.transaction(request_id) as tx:
            identities = tx.find(
                "external_identities",
                {"issuer": identity["issuer"], "subject": identity["subject"]},
            )
            if identities:
                account_id = identities[0]["account_id"]
                if tx.get("marketplace_accounts", {"id": account_id})["state"] != "active":
                    raise MarketplaceError("forbidden", 403)
            else:
                account_id = uuid4()
                tx.insert(
                    "marketplace_accounts",
                    {"id": account_id, "state": "active", "created_at": now()},
                )
                tx.insert(
                    "external_identities",
                    {
                        "id": uuid4(),
                        "account_id": account_id,
                        "issuer": identity["issuer"],
                        "subject": identity["subject"],
                        "created_at": now(),
                    },
                )
            tx.insert(
                "hosted_sessions",
                {
                    "token_hash": token_hash(session),
                    "account_id": account_id,
                    "authenticated_at": identity["authenticated_at"],
                    "acr": identity["acr"],
                    "amr": identity["amr"],
                    "csrf_hash": token_hash(csrf),
                    "created_at": now(),
                    "expires_at": now() + timedelta(seconds=self.session_seconds),
                    "idle_expires_at": now() + timedelta(seconds=self.idle_seconds),
                    "revoked": False,
                },
            )
            tx.update(
                "auth_transactions",
                {"state_hash": token_hash(state)},
                {"completed_account_id": account_id},
            )
            tx.audit(account_id, "session_issued", str(account_id), {})
        return session, csrf, value["redirect_uri"] is not None

    def authenticate(self, token: str, browser: bool, request_id: str) -> Actor:
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise MarketplaceError("authentication_required", 401)
        table = "hosted_sessions" if browser else "cli_credentials"
        with self.uow.transaction(request_id) as tx:
            rows = tx.find(table, {"token_hash": token_hash(token)})
            if not rows:
                raise MarketplaceError("authentication_required", 401)
            value = tx.get(table, {"token_hash": token_hash(token)})
            account = tx.get("marketplace_accounts", {"id": value["account_id"]})
            if (
                account["state"] != "active"
                or value["revoked"]
                or value["expires_at"] <= now()
                or (browser and value["idle_expires_at"] <= now())
            ):
                raise MarketplaceError("credential_expired", 401)
            if browser:
                tx.update(
                    table,
                    {"token_hash": token_hash(token)},
                    {
                        "idle_expires_at": min(
                            value["expires_at"], now() + timedelta(seconds=self.idle_seconds)
                        )
                    },
                )
            return Actor(
                value["account_id"],
                value["authenticated_at"],
                value["acr"],
                tuple(value["amr"]),
                "browser" if browser else value["scope"],
                token_hash(token),
                value.get("publisher_id"),
            )

    def csrf(self, actor: Actor, supplied: str) -> None:
        with self.uow.transaction() as tx:
            session = tx.get("hosted_sessions", {"token_hash": actor.credential_hash})
            if (
                not supplied
                or len(supplied) > 128
                or not hmac.compare_digest(session["csrf_hash"], token_hash(supplied))
            ):
                raise MarketplaceError("forbidden", 403)

    def consent(self, actor: Actor, state: str, browser: str, request_id: str) -> str:
        with self.uow.transaction(request_id) as tx:
            session = tx.get("hosted_sessions", {"token_hash": actor.credential_hash})
            if actor.scope != "browser" or session["revoked"] or session["expires_at"] <= now():
                raise MarketplaceError("authentication_required", 401)
            value = tx.get("auth_transactions", {"state_hash": token_hash(state)})
            if (
                not value["consumed"]
                or value["consented"]
                or value["expires_at"] <= now()
                or (
                    not hmac.compare_digest(value["browser_hash"], token_hash(browser))
                    or not value["redirect_uri"]
                    or value["completed_account_id"] != actor.account_id
                )
            ):
                raise MarketplaceError("authentication_required", 401)
            # Consent is issued only for the account that completed this browser transaction.
            # Bind the freshly minted callback session's account before issuing a code.
            code = secrets.token_urlsafe(32)
            tx.insert(
                "authorization_codes",
                {
                    "code_hash": token_hash(code),
                    "account_id": actor.account_id,
                    "client_id": "nervos-publisher-cli",
                    "redirect_uri": value["redirect_uri"],
                    "challenge": value["cli_challenge"],
                    "scope": value["scope"],
                    "publisher_id": value["publisher_id"],
                    "authenticated_at": actor.authenticated_at,
                    "acr": actor.acr,
                    "amr": list(actor.amr),
                    "expires_at": now() + timedelta(seconds=60),
                    "consumed": False,
                },
            )
            tx.update("auth_transactions", {"state_hash": token_hash(state)}, {"consented": True})
            tx.audit(
                actor.account_id,
                "authorization_code_issued",
                str(actor.account_id),
                {"scope": value["scope"]},
            )
            return (
                value["redirect_uri"] + "?" + urlencode({"code": code, "state": value["cli_state"]})
            )

    def exchange(
        self, code: str, verifier: str, callback: str, client_id: str, request_id: str
    ) -> Record:
        redirect_uri(callback)
        with self.uow.transaction(request_id) as tx:
            value = tx.get("authorization_codes", {"code_hash": token_hash(code)})
            if (
                value["consumed"]
                or value["expires_at"] <= now()
                or value["client_id"] != client_id
                or (
                    value["redirect_uri"] != callback
                    or not hmac.compare_digest(value["challenge"], pkce(verifier))
                )
            ):
                raise MarketplaceError("authentication_required", 401)
            if tx.get("marketplace_accounts", {"id": value["account_id"]})["state"] != "active":
                raise MarketplaceError("forbidden", 403)
            token = secrets.token_urlsafe(32)
            tx.insert(
                "cli_credentials",
                {
                    "token_hash": token_hash(token),
                    "account_id": value["account_id"],
                    "authenticated_at": value["authenticated_at"],
                    "acr": value["acr"],
                    "amr": value["amr"],
                    "scope": value["scope"],
                    "publisher_id": value["publisher_id"],
                    "created_at": now(),
                    "expires_at": now() + timedelta(seconds=self.token_seconds),
                    "revoked": False,
                },
            )
            tx.update("authorization_codes", {"code_hash": token_hash(code)}, {"consumed": True})
            tx.audit(
                value["account_id"],
                "credential_issued",
                str(value["account_id"]),
                {"scope": value["scope"]},
            )
            return {
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": self.token_seconds,
                "scope": value["scope"],
                "account_id": str(value["account_id"]),
            }

    def logout(self, actor: Actor, request_id: str) -> None:
        with self.uow.transaction(request_id) as tx:
            table = "hosted_sessions" if actor.scope == "browser" else "cli_credentials"
            tx.update(table, {"token_hash": actor.credential_hash}, {"revoked": True})
            tx.audit(actor.account_id, "credential_revoked", str(actor.account_id), {})
