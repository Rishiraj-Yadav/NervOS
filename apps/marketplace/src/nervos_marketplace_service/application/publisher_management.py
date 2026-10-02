"""Hosted publisher, project and public-key use cases; transport-independent."""

from __future__ import annotations

import base64
import json
import secrets
from datetime import timedelta
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from nervos_core.application.package_integrity import canonical_json_bytes, signature_fingerprint
from nervos_core.domain.packages import validate_package_id

from nervos_marketplace_service.application.authorization import Authorization
from nervos_marketplace_service.application.publication_ports import PublicationUnitOfWork
from nervos_marketplace_service.domain.errors import MarketplaceError
from nervos_marketplace_service.domain.identity import (
    Actor,
    Record,
    bounded_text,
    now,
    publisher_handle,
)

OWNERS = ("owner",)
MAINTAINERS = ("owner", "maintainer")
PUBLISHERS = ("owner", "maintainer", "publisher")


class PublisherManagement:
    def __init__(
        self,
        uow: PublicationUnitOfWork,
        authority: Authorization,
        audience: str,
        challenge_limit: int = 10,
        project_limit: int = 100,
    ) -> None:
        self.uow, self.authority, self.audience = uow, authority, audience
        self.challenge_limit, self.project_limit = challenge_limit, project_limit

    def create(
        self, actor: Actor, handle: str, display_name: str, kind: str, request_id: str
    ) -> Record:
        publisher_handle(handle)
        bounded_text(display_name, 256, 1024, nonempty=True)
        if kind not in {"individual", "organization"}:
            raise MarketplaceError("invalid_request")
        value: Record = {
            "id": uuid4(),
            "handle": handle,
            "display_name": display_name,
            "kind": kind,
            # MVP onboarding is self-service; production moderation can be
            # layered on without changing project ownership semantics.
            "state": "active",
            "revision": 1,
            "created_at": now(),
            "updated_at": now(),
        }
        with self.uow.transaction(request_id) as tx:
            self.authority.account(tx, actor)
            if actor.scope not in {"publisher", "browser"} or actor.publisher_id is not None:
                raise MarketplaceError("forbidden", 403)
            tx.rate_limit(actor.account_id, "publisher_created", 10)
            tx.insert("publishers", value)
            tx.insert(
                "publisher_memberships",
                {
                    "publisher_id": value["id"],
                    "account_id": actor.account_id,
                    "role": "owner",
                    "state": "active",
                    "created_at": now(),
                    "updated_at": now(),
                },
            )
            tx.audit(actor.account_id, "publisher_created", str(value["id"]), {"handle": handle})
        return value

    def show(self, actor: Actor, publisher_id: UUID, request_id: str) -> Record:
        with self.uow.transaction(request_id) as tx:
            return self.authority.publisher(tx, actor, publisher_id, PUBLISHERS, active=False)

    def mine(self, actor: Actor, request_id: str) -> list[Record]:
        with self.uow.transaction(request_id) as tx:
            self.authority.account(tx, actor)
            memberships = tx.find(
                "publisher_memberships", {"account_id": actor.account_id, "state": "active"}
            )
            return [
                tx.get("publishers", {"id": item["publisher_id"]}, lock=False)
                for item in memberships
                if actor.publisher_id is None or actor.publisher_id == item["publisher_id"]
            ]

    def update(
        self, actor: Actor, publisher_id: UUID, display_name: str, revision: int, request_id: str
    ) -> Record:
        bounded_text(display_name, 256, 1024, nonempty=True)
        with self.uow.transaction(request_id) as tx:
            value = self.authority.publisher(tx, actor, publisher_id, OWNERS)
            if value["revision"] != revision:
                raise MarketplaceError("revision_conflict", 409)
            changes = {"display_name": display_name, "revision": revision + 1, "updated_at": now()}
            tx.update("publishers", {"id": publisher_id}, changes)
            tx.audit(
                actor.account_id, "publisher_updated", str(publisher_id), {"revision": revision + 1}
            )
            return {**value, **changes}

    def membership(
        self,
        actor: Actor,
        publisher_id: UUID,
        account_id: UUID,
        role: str,
        removed: bool,
        request_id: str,
    ) -> None:
        if role not in PUBLISHERS:
            raise MarketplaceError("invalid_request")
        with self.uow.transaction(request_id) as tx:
            self.authority.publisher(tx, actor, publisher_id, OWNERS, sensitive=True)
            tx.get("marketplace_accounts", {"id": account_id}, lock=False)
            members = tx.find("publisher_memberships", {"publisher_id": publisher_id})
            old = next((item for item in members if item["account_id"] == account_id), None)
            if (
                old
                and old["role"] == "owner"
                and old["state"] == "active"
                and (removed or role != "owner")
                and not any(
                    item["account_id"] != account_id
                    and item["role"] == "owner"
                    and item["state"] == "active"
                    for item in members
                )
            ):
                raise MarketplaceError("last_owner", 409)
            if len(members) >= 100 and old is None:
                raise MarketplaceError("quota_exceeded", 429)
            changes = {
                "role": role,
                "state": "removed" if removed else "active",
                "updated_at": now(),
            }
            where = {"publisher_id": publisher_id, "account_id": account_id}
            if old:
                tx.update("publisher_memberships", where, changes)
            else:
                tx.insert("publisher_memberships", {**where, **changes, "created_at": now()})
            tx.audit(
                actor.account_id,
                "membership_changed",
                str(publisher_id),
                {"account_id": str(account_id), "role": role, "state": changes["state"]},
            )

    def moderate(
        self, actor: Actor, publisher_id: UUID, state: str, reason: str, request_id: str
    ) -> None:
        if state not in {"active", "suspended"}:
            raise MarketplaceError("invalid_request")
        bounded_text(reason, 4096, 4096, nonempty=True)
        with self.uow.transaction(request_id) as tx:
            self.authority.operator(tx, actor)
            value = tx.get("publishers", {"id": publisher_id})
            if value["state"] == "tombstoned":
                raise MarketplaceError("invalid_state_transition", 409)
            tx.update(
                "publishers",
                {"id": publisher_id},
                {"state": state, "revision": value["revision"] + 1, "updated_at": now()},
            )
            for project in tx.find("package_projects", {"publisher_id": publisher_id}):
                tx.update(
                    "package_projects",
                    {"id": project["id"]},
                    {"visibility": "hidden" if state == "suspended" else "visible"},
                )
            tx.audit(
                actor.account_id,
                "publisher_moderated",
                str(publisher_id),
                {"state": state, "reason": reason},
            )

    def disable_account(self, actor: Actor, account_id: UUID, reason: str, request_id: str) -> None:
        bounded_text(reason, 4096, 4096, nonempty=True)
        with self.uow.transaction(request_id) as tx:
            self.authority.operator(tx, actor)
            tx.get("marketplace_accounts", {"id": account_id})
            tx.update("marketplace_accounts", {"id": account_id}, {"state": "disabled"})
            tx.audit(actor.account_id, "account_disabled", str(account_id), {"reason": reason})

    def claim(self, actor: Actor, publisher_id: UUID, package_id: str, request_id: str) -> Record:
        validate_package_id(package_id)
        value: Record = {
            "id": uuid4(),
            "publisher_id": publisher_id,
            "package_id": package_id,
            "ownership_revision": 1,
            "visibility": "visible",
            "created_at": now(),
        }
        with self.uow.transaction(request_id) as tx:
            self.authority.publisher(tx, actor, publisher_id, OWNERS, sensitive=True)
            if tx.find("package_projects", {"package_id": package_id}):
                raise MarketplaceError("package_id_claimed", 409)
            if (
                len(tx.find("package_projects", {"publisher_id": publisher_id}))
                >= self.project_limit
            ):
                raise MarketplaceError("quota_exceeded", 429)
            tx.insert("package_projects", value)
            tx.insert(
                "package_listings",
                {
                    "project_id": value["id"],
                    "display_name": package_id,
                    "summary": "",
                    "description": "",
                    "revision": 1,
                    "updated_at": now(),
                },
            )
            tx.audit(
                actor.account_id, "project_claimed", str(value["id"]), {"package_id": package_id}
            )
        return value

    def project(self, actor: Actor, project_id: UUID, request_id: str) -> Record:
        with self.uow.transaction(request_id) as tx:
            return self.authority.project(tx, actor, project_id, PUBLISHERS)

    def transfer(
        self, actor: Actor, project_id: UUID, destination: UUID, revision: int, request_id: str
    ) -> Record:
        with self.uow.transaction(request_id) as tx:
            initial = tx.get("package_projects", {"id": project_id}, lock=False)
            for identifier in sorted({initial["publisher_id"], destination}, key=str):
                tx.get("publishers", {"id": identifier})
            project = self.authority.project(
                tx, actor, project_id, OWNERS, sensitive=True, revision=revision
            )
            if destination == project["publisher_id"]:
                raise MarketplaceError("invalid_request")
            target = tx.get("publishers", {"id": destination})
            if target["state"] != "active":
                raise MarketplaceError("publisher_suspended", 403)
            value: Record = {
                "id": uuid4(),
                "project_id": project_id,
                "source_publisher_id": project["publisher_id"],
                "destination_publisher_id": destination,
                "ownership_revision": revision,
                "requester_id": actor.account_id,
                "accepting_id": None,
                "state": "pending",
                "created_at": now(),
                "expires_at": now() + timedelta(hours=24),
            }
            tx.insert("project_transfers", value)
            tx.audit(
                actor.account_id,
                "transfer_requested",
                str(project_id),
                {"transfer_id": str(value["id"])},
            )
            return value

    def finish_transfer(
        self, actor: Actor, transfer_id: UUID, accept: bool, request_id: str
    ) -> None:
        with self.uow.transaction(request_id) as tx:
            self.authority.account(tx, actor)
            initial = tx.get("project_transfers", {"id": transfer_id}, lock=False)
            for identifier in sorted(
                {initial["source_publisher_id"], initial["destination_publisher_id"]}, key=str
            ):
                tx.get("publishers", {"id": identifier})
            owner = (
                initial["destination_publisher_id"] if accept else initial["source_publisher_id"]
            )
            self.authority.publisher(tx, actor, owner, OWNERS, sensitive=True)
            transfer = tx.get("project_transfers", {"id": transfer_id})
            project = tx.get("package_projects", {"id": transfer["project_id"]})
            if transfer["state"] != "pending" or transfer["expires_at"] <= now():
                raise MarketplaceError("invalid_state_transition", 409)
            if project["publisher_id"] != transfer["source_publisher_id"] or (
                project["ownership_revision"] != transfer["ownership_revision"]
            ):
                raise MarketplaceError("ownership_conflict", 409)
            if accept:
                tx.update(
                    "package_projects",
                    {"id": project["id"]},
                    {
                        "publisher_id": owner,
                        "ownership_revision": project["ownership_revision"] + 1,
                    },
                )
                for key in tx.find("project_key_authorizations", {"project_id": project["id"]}):
                    tx.update(
                        "project_key_authorizations",
                        {"project_id": project["id"], "key_id": key["key_id"]},
                        {"state": "revoked"},
                    )
                # Verification completion checks captured ownership; no old lease can reserve.
            tx.update(
                "project_transfers",
                {"id": transfer_id},
                {"state": "accepted" if accept else "cancelled", "accepting_id": actor.account_id},
            )
            tx.audit(
                actor.account_id,
                "transfer_accepted" if accept else "transfer_cancelled",
                str(project["id"]),
                {"transfer_id": str(transfer_id)},
            )

    def challenge(
        self, actor: Actor, publisher_id: UUID, public_key: bytes, request_id: str
    ) -> Record:
        try:
            Ed25519PublicKey.from_public_bytes(public_key)
        except ValueError:
            raise MarketplaceError("invalid_request") from None
        identifier, expires = uuid4(), now() + timedelta(minutes=5)
        payload = canonical_json_bytes(
            {
                "domain": "nervos.marketplace.key-pop.v1",
                "audience": self.audience,
                "publisher_id": str(publisher_id),
                "account_id": str(actor.account_id),
                "fingerprint": signature_fingerprint(public_key),
                "challenge_id": str(identifier),
                "nonce": secrets.token_urlsafe(32),
                "action": "register",
                "expires_at": expires.isoformat(),
            }
        )
        with self.uow.transaction(request_id) as tx:
            self.authority.publisher(tx, actor, publisher_id, OWNERS, sensitive=True)
            tx.rate_limit(actor.account_id, "key_challenge", self.challenge_limit)
            tx.insert(
                "key_proof_challenges",
                {
                    "id": identifier,
                    "publisher_id": publisher_id,
                    "account_id": actor.account_id,
                    "public_key": public_key,
                    "payload": payload,
                    "expires_at": expires,
                    "consumed": False,
                },
            )
            tx.audit(
                actor.account_id,
                "key_challenge",
                str(publisher_id),
                {"challenge_id": str(identifier)},
            )
        return {
            "id": identifier,
            "payload": base64.b64encode(payload).decode(),
            "expires_at": expires,
        }

    def prove(
        self,
        actor: Actor,
        publisher_id: UUID,
        challenge_id: UUID,
        signature: bytes,
        request_id: str,
    ) -> Record:
        with self.uow.transaction(request_id) as tx:
            self.authority.publisher(tx, actor, publisher_id, OWNERS, sensitive=True)
            challenge = tx.get("key_proof_challenges", {"id": challenge_id})
            if (
                challenge["consumed"]
                or challenge["expires_at"] <= now()
                or (
                    challenge["publisher_id"] != publisher_id
                    or challenge["account_id"] != actor.account_id
                )
            ):
                raise MarketplaceError("forbidden", 403)
            payload = json.loads(bytes(challenge["payload"]))
            if payload["audience"] != self.audience or payload["action"] != "register":
                raise MarketplaceError("forbidden", 403)
            try:
                Ed25519PublicKey.from_public_bytes(bytes(challenge["public_key"])).verify(
                    signature, bytes(challenge["payload"])
                )
            except (InvalidSignature, ValueError):
                raise MarketplaceError("signature_invalid", 422) from None
            fingerprint = signature_fingerprint(bytes(challenge["public_key"]))
            existing = tx.find(
                "publisher_signing_keys", {"publisher_id": publisher_id, "fingerprint": fingerprint}
            )
            if existing:
                raise MarketplaceError("invalid_state_transition", 409)
            value: Record = {
                "id": uuid4(),
                "publisher_id": publisher_id,
                "public_key": bytes(challenge["public_key"]),
                "fingerprint": fingerprint,
                "state": "active",
                "created_at": now(),
                "updated_at": now(),
            }
            tx.insert("publisher_signing_keys", value)
            tx.update("key_proof_challenges", {"id": challenge_id}, {"consumed": True})
            tx.audit(
                actor.account_id, "key_registered", str(value["id"]), {"fingerprint": fingerprint}
            )
            return {key: value[key] for key in ("id", "publisher_id", "fingerprint", "state")}

    def keys(self, actor: Actor, publisher_id: UUID, request_id: str) -> list[Record]:
        with self.uow.transaction(request_id) as tx:
            self.authority.publisher(tx, actor, publisher_id, PUBLISHERS, active=False)
            return [
                {k: row[k] for k in ("id", "fingerprint", "state", "created_at")}
                for row in tx.find("publisher_signing_keys", {"publisher_id": publisher_id})
            ]

    def key_state(
        self, actor: Actor, publisher_id: UUID, key_id: UUID, state: str, request_id: str
    ) -> None:
        if state not in {"retired", "revoked"}:
            raise MarketplaceError("invalid_request")
        with self.uow.transaction(request_id) as tx:
            self.authority.publisher(tx, actor, publisher_id, OWNERS, sensitive=True)
            key = tx.get("publisher_signing_keys", {"id": key_id, "publisher_id": publisher_id})
            if key["state"] == "revoked" or (key["state"] == "retired" and state == "retired"):
                raise MarketplaceError("invalid_state_transition", 409)
            tx.update(
                "publisher_signing_keys", {"id": key_id}, {"state": state, "updated_at": now()}
            )
            tx.audit(
                actor.account_id, "key_" + state, str(key_id), {"fingerprint": key["fingerprint"]}
            )

    def authorize_key(
        self, actor: Actor, project_id: UUID, key_id: UUID, revision: int, request_id: str
    ) -> None:
        with self.uow.transaction(request_id) as tx:
            project = self.authority.project(
                tx, actor, project_id, OWNERS, sensitive=True, revision=revision
            )
            key = tx.get(
                "publisher_signing_keys", {"id": key_id, "publisher_id": project["publisher_id"]}
            )
            if key["state"] != "active":
                raise MarketplaceError("key_" + key["state"], 403)
            where = {"project_id": project_id, "key_id": key_id}
            if tx.find("project_key_authorizations", where):
                raise MarketplaceError("invalid_state_transition", 409)
            tx.insert(
                "project_key_authorizations",
                {
                    **where,
                    "ownership_revision": revision,
                    "state": "active",
                    "actor_id": actor.account_id,
                    "created_at": now(),
                },
            )
            tx.audit(
                actor.account_id,
                "key_authorized",
                str(project_id),
                {"key_id": str(key_id), "revision": revision},
            )
