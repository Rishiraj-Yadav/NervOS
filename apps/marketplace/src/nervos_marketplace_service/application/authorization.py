"""One current-authority decision, reused at admission, completion and publication."""

from uuid import UUID

from nervos_marketplace_service.application.publication_ports import IdentityTransaction
from nervos_marketplace_service.domain.errors import MarketplaceError
from nervos_marketplace_service.domain.identity import Actor, Assurance, Record, now


class Authorization:
    def __init__(self, assurance: Assurance) -> None:
        self.assurance = assurance

    def account(self, tx: IdentityTransaction, actor: Actor) -> None:
        account = tx.get("marketplace_accounts", {"id": actor.account_id})
        if account["state"] != "active":
            raise MarketplaceError("forbidden", 403)
        table = "cli_credentials" if actor.scope != "browser" else "hosted_sessions"
        credential = tx.get(table, {"token_hash": actor.credential_hash})
        if credential["revoked"] or credential["expires_at"] <= now():
            raise MarketplaceError("credential_expired", 401)

    def publisher(
        self,
        tx: IdentityTransaction,
        actor: Actor,
        publisher_id: UUID,
        roles: tuple[str, ...],
        *,
        sensitive: bool = False,
        active: bool = True,
    ) -> Record:
        self.account(tx, actor)
        if actor.scope not in {"publisher", "browser"} or (
            actor.publisher_id is not None and actor.publisher_id != publisher_id
        ):
            raise MarketplaceError("forbidden", 403)
        publisher = tx.get("publishers", {"id": publisher_id})
        rows = tx.find(
            "publisher_memberships",
            {"publisher_id": publisher_id, "account_id": actor.account_id, "state": "active"},
        )
        if not rows or rows[0]["role"] not in roles:
            raise MarketplaceError("forbidden", 403)
        if active and publisher["state"] != "active":
            code = "publisher_pending" if publisher["state"] == "pending" else "publisher_suspended"
            raise MarketplaceError(code, 403)
        if sensitive:
            self.assurance.require(actor)
        return publisher

    def project(
        self,
        tx: IdentityTransaction,
        actor: Actor,
        project_id: UUID,
        roles: tuple[str, ...],
        *,
        sensitive: bool = False,
        revision: int | None = None,
    ) -> Record:
        # Nonlocking lookup obtains the owner; recheck after locking publisher then project.
        initial = tx.get("package_projects", {"id": project_id}, lock=False)
        self.publisher(tx, actor, initial["publisher_id"], roles, sensitive=sensitive)
        project = tx.get("package_projects", {"id": project_id})
        if project["publisher_id"] != initial["publisher_id"] or (
            revision is not None and revision != project["ownership_revision"]
        ):
            raise MarketplaceError("ownership_conflict", 409)
        return project

    def operator(self, tx: IdentityTransaction, actor: Actor) -> None:
        self.account(tx, actor)
        if actor.scope not in {"operator", "browser"}:
            raise MarketplaceError("forbidden", 403)
        grants = tx.find("marketplace_operator_grants", {"account_id": actor.account_id})
        if not grants or not grants[0]["active"]:
            raise MarketplaceError("forbidden", 403)
        self.assurance.require(actor)
