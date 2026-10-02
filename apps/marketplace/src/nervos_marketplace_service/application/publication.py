"""Durable uploads and explicit publication. No agent/runtime/installer access."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from nervos_core.application.package_integrity import canonical_json_bytes
from nervos_core.domain.package_installation import validate_sha256
from nervos_core.domain.packages import PackageVersion

from nervos_marketplace_service.application.authorization import Authorization
from nervos_marketplace_service.application.publication_ports import (
    IdentityTransaction,
    PublicationStorage,
    PublicationUnitOfWork,
    StaticPackageVerifier,
)
from nervos_marketplace_service.application.publisher_management import (
    MAINTAINERS,
    OWNERS,
    PUBLISHERS,
)
from nervos_marketplace_service.domain.artifact_reference import FinalizedArtifactRef
from nervos_marketplace_service.domain.errors import MarketplaceError
from nervos_marketplace_service.domain.identity import Actor, Record, bounded_text, now
from nervos_marketplace_service.domain.semver_order import precedence_key


def operation_view(value: Record) -> Record:
    return {
        key: value[key]
        for key in (
            "id",
            "project_id",
            "state",
            "archive_sha256",
            "size_bytes",
            "ownership_revision",
            "failure_code",
            "attempt_count",
            "ready_release_id",
            "expires_at",
        )
    }


class Publication:
    def __init__(
        self,
        uow: PublicationUnitOfWork,
        authority: Authorization,
        storage: PublicationStorage,
        root: Path,
        *,
        timeout: int = 300,
        idle: int = 15,
        account_uploads: int = 2,
        publisher_uploads: int = 4,
        daily_bytes: int = 2 * 1024**3,
    ) -> None:
        self.uow, self.authority, self.storage = uow, authority, storage
        self.root = root
        self.timeout, self.idle = timeout, idle
        self.account_uploads, self.publisher_uploads, self.daily_bytes = (
            account_uploads,
            publisher_uploads,
            daily_bytes,
        )

    def create_upload(
        self,
        actor: Actor,
        project_id: UUID,
        digest: str,
        size: int,
        revision: int,
        key: str,
        request_id: str,
    ) -> Record:
        validate_sha256(digest)
        if (
            actor.scope != "publisher"
            or not 0 < size <= 256 * 1024**2
            or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key) is None
        ):
            raise MarketplaceError("invalid_request")
        request_digest = hashlib.sha256(
            canonical_json_bytes(
                {"project": str(project_id), "digest": digest, "size": size, "revision": revision}
            )
        ).hexdigest()
        with self.uow.transaction(request_id) as tx:
            project = self.authority.project(tx, actor, project_id, PUBLISHERS, revision=revision)
            where = {
                "actor_id": actor.account_id,
                "action": "upload",
                "target": str(project_id),
                "key": key,
            }
            existing = tx.find("publication_idempotency", where)
            if existing:
                if existing[0]["request_digest"] != request_digest:
                    raise MarketplaceError("idempotency_conflict", 409)
                return operation_view(tx.get("upload_operations", {"id": existing[0]["result_id"]}))
            active_actor, active_pub, daily = tx.upload_capacity(
                actor.account_id, project["publisher_id"]
            )
            if (
                active_actor >= self.account_uploads
                or active_pub >= self.publisher_uploads
                or daily + size > self.daily_bytes
            ):
                raise MarketplaceError("quota_exceeded", 429)
            identifier = uuid4()
            value: Record = {
                "id": identifier,
                "actor_id": actor.account_id,
                "publisher_id": project["publisher_id"],
                "project_id": project_id,
                "ownership_revision": revision,
                "archive_sha256": digest,
                "size_bytes": size,
                "quarantine_key": f"quarantine/{identifier}/{digest}.nervos",
                "state": "uploading",
                "failure_code": None,
                "attempt_count": 0,
                "generation": 0,
                "ready_release_id": None,
                "created_at": now(),
                "updated_at": now(),
                "expires_at": now() + timedelta(hours=24),
                "credential_hash": actor.credential_hash,
            }
            tx.insert("upload_operations", value)
            tx.insert(
                "publication_idempotency",
                {
                    **where,
                    "request_digest": request_digest,
                    "result_id": identifier,
                    "expires_at": now() + timedelta(days=7),
                },
            )
            tx.audit(
                actor.account_id,
                "upload_admitted",
                str(identifier),
                {"digest": digest, "size": size},
            )
            return operation_view(value)

    def _operation(self, tx: IdentityTransaction, actor: Actor, identifier: UUID) -> Record:
        initial = tx.get("upload_operations", {"id": identifier}, lock=False)
        self.authority.project(
            tx, actor, initial["project_id"], PUBLISHERS, revision=initial["ownership_revision"]
        )
        value = tx.get("upload_operations", {"id": identifier})
        if value["actor_id"] != actor.account_id:
            raise MarketplaceError("forbidden", 403)
        if value["expires_at"] <= now():
            raise MarketplaceError("upload_expired", 409)
        return value

    def status(self, actor: Actor, identifier: UUID, request_id: str) -> Record:
        with self.uow.transaction(request_id) as tx:
            value = self._operation(tx, actor, identifier)
            result = operation_view(value)
            if value["ready_release_id"]:
                release = tx.get("package_releases", {"id": value["ready_release_id"]}, lock=False)
                result["release"] = {
                    key: release[key]
                    for key in (
                        "id",
                        "exact_version",
                        "archive_sha256",
                        "content_digest",
                        "signer_fingerprint",
                        "publication_state",
                    )
                }
            return result

    async def receive(
        self,
        actor: Actor,
        identifier: UUID,
        source: AsyncIterator[bytes],
        length: int | None,
        request_id: str,
    ) -> Record:
        if length is None:
            raise MarketplaceError("length_required", 411)
        if length > 256 * 1024**2:
            raise MarketplaceError("upload_too_large", 413)
        upload_token = uuid4()
        with self.uow.transaction(request_id) as tx:
            value = self._operation(tx, actor, identifier)
            if length != value["size_bytes"]:
                raise MarketplaceError("upload_invalid", 422)
            if value["state"] != "uploading" or (
                value.get("upload_expires_at") and value["upload_expires_at"] > now()
            ):
                raise MarketplaceError("invalid_state_transition", 409)
            tx.update(
                "upload_operations",
                {"id": identifier},
                {
                    "upload_token": upload_token,
                    "upload_expires_at": now() + timedelta(seconds=self.timeout + 30),
                    "credential_hash": actor.credential_hash,
                },
            )
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with tempfile.TemporaryDirectory(prefix="mp-upload-", dir=self.root) as directory:
                path = Path(directory) / "archive.nervos"
                digest, size = hashlib.sha256(), 0
                started = time.monotonic()
                with path.open("wb") as output:
                    while True:
                        remaining = self.timeout - (time.monotonic() - started)
                        if remaining <= 0:
                            raise MarketplaceError("upload_invalid", 422)
                        try:
                            chunk = await asyncio.wait_for(anext(source), min(self.idle, remaining))
                        except StopAsyncIteration:
                            break
                        size += len(chunk)
                        if size > length:
                            raise MarketplaceError("upload_invalid", 422)
                        for offset in range(0, len(chunk), 65536):
                            block = chunk[offset : offset + 65536]
                            digest.update(block)
                            await asyncio.to_thread(output.write, block)
                    await asyncio.to_thread(output.flush)
                    await asyncio.to_thread(os.fsync, output.fileno())
                if size != length or digest.hexdigest() != value["archive_sha256"]:
                    raise MarketplaceError("upload_invalid", 422)
                await asyncio.to_thread(
                    self.storage.quarantine, identifier, value["archive_sha256"], path, size
                )
                with self.uow.transaction(request_id) as tx:
                    current = self._operation(tx, actor, identifier)
                    if current["upload_token"] != upload_token:
                        raise MarketplaceError("ownership_conflict", 409)
                    tx.update(
                        "upload_operations",
                        {"id": identifier},
                        {
                            "state": "uploaded",
                            "updated_at": now(),
                            "upload_token": None,
                            "upload_expires_at": None,
                        },
                    )
                    tx.audit(
                        actor.account_id,
                        "upload_completed",
                        str(identifier),
                        {"digest": value["archive_sha256"], "size": size},
                    )
                return self.status(actor, identifier, request_id)
        except BaseException as error:
            with self.uow.transaction(request_id) as tx:
                current = tx.get("upload_operations", {"id": identifier})
                if current.get("upload_token") == upload_token:
                    tx.update(
                        "upload_operations",
                        {"id": identifier},
                        {
                            "upload_token": None,
                            "upload_expires_at": None,
                            "failure_code": "upload_invalid",
                            "updated_at": now(),
                        },
                    )
                    tx.audit(actor.account_id, "upload_interrupted", str(identifier), {})
            if isinstance(
                error, (MarketplaceError, asyncio.CancelledError, KeyboardInterrupt, SystemExit)
            ):
                raise
            raise MarketplaceError("upload_invalid", 422) from None

    def signer(self, tx: IdentityTransaction, project: Record, fingerprint: str) -> Record:
        keys = tx.find(
            "publisher_signing_keys",
            {"publisher_id": project["publisher_id"], "fingerprint": fingerprint},
        )
        if not keys or keys[0]["state"] != "active":
            raise MarketplaceError("signer_not_authorized", 403)
        key = tx.get("publisher_signing_keys", {"id": keys[0]["id"]})
        grants = tx.find(
            "project_key_authorizations", {"project_id": project["id"], "key_id": key["id"]}
        )
        if (
            not grants
            or grants[0]["state"] != "active"
            or grants[0]["ownership_revision"] != project["ownership_revision"]
        ):
            raise MarketplaceError("key_not_authorized", 403)
        return key

    async def verify_upload(
        self, actor: Actor, operation_id: UUID, verifier: StaticPackageVerifier, request_id: str
    ) -> Record:
        """Fenced singleton parser admission, heartbeat and recoverable expired claims."""
        lease = uuid4()
        with self.uow.transaction(request_id) as tx:
            operation = self._operation(tx, actor, operation_id)
            if operation["state"] == "verified":
                return operation_view(operation)
            if operation["state"] != "uploaded" and not (
                operation["state"] == "verifying" and operation["lease_expires_at"] <= now()
            ):
                raise MarketplaceError("invalid_state_transition", 409)
            gate = tx.get("verification_admission", {"id": 1})
            if gate["operation_id"] and gate["expires_at"] > now():
                raise MarketplaceError("verifier_busy", 429)
            if operation["attempt_count"] >= 3:
                raise MarketplaceError("verification_attempts_exhausted", 409)
            generation = operation["generation"] + 1
            expiry = now() + timedelta(seconds=60)
            tx.update(
                "upload_operations",
                {"id": operation_id},
                {
                    "state": "verifying",
                    "generation": generation,
                    "lease_token": lease,
                    "lease_expires_at": expiry,
                    "attempt_count": operation["attempt_count"] + 1,
                },
            )
            tx.update(
                "verification_admission",
                {"id": 1},
                {
                    "operation_id": operation_id,
                    "generation": generation,
                    "expires_at": expiry,
                },
            )
            tx.audit(
                actor.account_id,
                "verification_started",
                str(operation_id),
                {"generation": generation},
            )

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(10)
                with self.uow.transaction(request_id) as tx:
                    current = tx.get("upload_operations", {"id": operation_id})
                    if current["lease_token"] != lease or current["generation"] != generation:
                        raise MarketplaceError("ownership_conflict", 409)
                    expiry = now() + timedelta(seconds=60)
                    tx.update(
                        "upload_operations", {"id": operation_id}, {"lease_expires_at": expiry}
                    )
                    gate = tx.get("verification_admission", {"id": 1})
                    if gate["operation_id"] != operation_id or gate["generation"] != generation:
                        raise MarketplaceError("ownership_conflict", 409)
                    tx.update("verification_admission", {"id": 1}, {"expires_at": expiry})

        pulse = asyncio.create_task(heartbeat())
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(prefix="mp-verify-", dir=self.root) as directory:
                archive = Path(directory) / "archive.nervos"
                await asyncio.to_thread(
                    self.storage.retrieve,
                    operation_id,
                    operation["archive_sha256"],
                    operation["size_bytes"],
                    archive,
                )
                evidence = await asyncio.to_thread(verifier.verify, archive)
                if pulse.done():
                    await pulse
                reference = await asyncio.to_thread(
                    self.storage.finalize,
                    operation["archive_sha256"],
                    archive,
                    operation["size_bytes"],
                )
                self.reserve(
                    actor, operation_id, generation, lease, evidence, reference, request_id
                )
            await asyncio.to_thread(
                self.storage.delete_quarantine, operation_id, operation["archive_sha256"]
            )
            return self.status(actor, operation_id, request_id)
        except Exception:
            with self.uow.transaction(request_id) as tx:
                current = tx.get("upload_operations", {"id": operation_id})
                if current["lease_token"] == lease and current["state"] == "verifying":
                    tx.update(
                        "upload_operations",
                        {"id": operation_id},
                        {
                            "state": "uploaded",
                            "failure_code": "verification_failed",
                            "lease_token": None,
                            "lease_expires_at": None,
                        },
                    )
                    tx.audit(actor.account_id, "verification_failed", str(operation_id), {})
            raise
        finally:
            pulse.cancel()
            with suppress(asyncio.CancelledError):
                await pulse
            with self.uow.transaction(request_id) as tx:
                gate = tx.get("verification_admission", {"id": 1})
                if gate["operation_id"] == operation_id and gate["generation"] == generation:
                    tx.update(
                        "verification_admission",
                        {"id": 1},
                        {
                            "operation_id": None,
                            "generation": None,
                            "expires_at": None,
                        },
                    )

    def reserve(
        self,
        actor: Actor,
        operation_id: UUID,
        generation: int,
        lease: UUID,
        evidence: Record,
        reference: FinalizedArtifactRef,
        request_id: str,
    ) -> UUID:
        with self.uow.transaction(request_id) as tx:
            operation = self._operation(tx, actor, operation_id)
            if (
                operation["state"] != "verifying"
                or operation["generation"] != generation
                or (operation["lease_token"] != lease or operation["lease_expires_at"] <= now())
            ):
                raise MarketplaceError("ownership_conflict", 409)
            project = tx.get("package_projects", {"id": operation["project_id"]})
            if (reference.archive_sha256, reference.size_bytes) != (
                operation["archive_sha256"],
                operation["size_bytes"],
            ) or evidence["size_bytes"] != reference.size_bytes:
                raise MarketplaceError("package_invalid", 422)
            if (
                evidence["package_id"] != project["package_id"]
                or evidence["archive_sha256"] != operation["archive_sha256"]
            ):
                raise MarketplaceError("package_invalid", 422)
            self.signer(tx, project, evidence["signer_fingerprint"])
            manifest = __import__("base64").b64decode(evidence["manifest_bytes"], validate=True)
            existing = tx.find(
                "package_releases",
                {"project_id": project["id"], "exact_version": evidence["exact_version"]},
            )
            if existing:
                release = existing[0]
                if any(
                    release[key] != evidence[key]
                    for key in ("archive_sha256", "content_digest", "signer_fingerprint")
                ) or (bytes(release["manifest_bytes"]) != manifest):
                    raise MarketplaceError("version_conflict", 409)
                identifier = release["id"]
            else:
                parsed = PackageVersion(evidence["exact_version"])
                identifier = uuid4()
                artifacts = tx.find("artifacts", {"archive_sha256": evidence["archive_sha256"]})
                if artifacts and (
                    artifacts[0]["storage_version_id"] != reference.version_id
                    or artifacts[0]["size_bytes"] != reference.size_bytes
                ):
                    raise MarketplaceError("version_conflict", 409)
                if not artifacts:
                    tx.insert(
                        "artifacts",
                        {
                            "archive_sha256": evidence["archive_sha256"],
                            "size_bytes": reference.size_bytes,
                            "storage_version_id": reference.version_id,
                            "created_at": now(),
                        },
                    )
                release = {
                    key: evidence[key]
                    for key in (
                        "exact_version",
                        "archive_sha256",
                        "content_digest",
                        "signer_fingerprint",
                        "manifest_version",
                        "runtime_language",
                        "runtime_python",
                        "nervos_min_version",
                        "nervos_max_version",
                    )
                }
                release.update(
                    {
                        "id": identifier,
                        "project_id": project["id"],
                        "original_publisher_id": project["publisher_id"],
                        "manifest_bytes": manifest,
                        "semver_key": precedence_key(parsed),
                        "is_prerelease": bool(parsed.prerelease),
                        "publication_state": "ready",
                        "distribution_state": "available",
                        "status_revision": 1,
                        "published_at": None,
                        "status_updated_at": now(),
                    }
                )
                tx.insert("package_releases", release)
            tx.update(
                "upload_operations",
                {"id": operation_id},
                {
                    "state": "verified",
                    "ready_release_id": identifier,
                    "updated_at": now(),
                    "failure_code": None,
                },
            )
            tx.audit(
                actor.account_id,
                "release_reserved",
                str(identifier),
                {
                    "archive_sha256": evidence["archive_sha256"],
                    "content_digest": evidence["content_digest"],
                },
            )
            return identifier

    def publish(
        self,
        actor: Actor,
        project_id: UUID,
        version: str,
        release_id: UUID,
        archive: str,
        content: str,
        revision: int,
        request_id: str,
    ) -> Record:
        PackageVersion(version)
        validate_sha256(archive)
        validate_sha256(content)
        # Network/object I/O occurs before the short authoritative transaction.
        with self.uow.transaction(request_id) as tx:
            self.authority.project(tx, actor, project_id, PUBLISHERS, revision=revision)
            initial = tx.get(
                "package_releases",
                {"id": release_id, "project_id": project_id, "exact_version": version},
                lock=False,
            )
            artifact = tx.get(
                "artifacts", {"archive_sha256": initial["archive_sha256"]}, lock=False
            )
        if not artifact["storage_version_id"]:
            raise MarketplaceError("artifact_unavailable", 503)
        reference = FinalizedArtifactRef(
            archive, artifact["size_bytes"], artifact["storage_version_id"]
        )
        if not self.storage.available(reference):
            raise MarketplaceError("artifact_unavailable", 503)
        with self.uow.transaction(request_id) as tx:
            project = self.authority.project(tx, actor, project_id, PUBLISHERS, revision=revision)
            release = tx.get(
                "package_releases",
                {"id": release_id, "project_id": project_id, "exact_version": version},
            )
            if release["archive_sha256"] != archive or release["content_digest"] != content:
                raise MarketplaceError("version_conflict", 409)
            self.signer(tx, project, release["signer_fingerprint"])
            if release["original_publisher_id"] != project["publisher_id"]:
                raise MarketplaceError("ownership_conflict", 409)
            if release["publication_state"] == "ready":
                tx.update(
                    "package_releases",
                    {"id": release_id},
                    {"publication_state": "published", "published_at": now()},
                )
                tx.audit(
                    actor.account_id,
                    "release_published",
                    str(release_id),
                    {"archive_sha256": archive, "content_digest": content},
                )
            return {
                "id": release_id,
                "exact_version": version,
                "archive_sha256": archive,
                "content_digest": content,
                "signer_fingerprint": release["signer_fingerprint"],
                "publication_state": "published",
            }

    def listing(
        self,
        actor: Actor,
        project_id: UUID,
        display_name: str,
        summary: str,
        description: str,
        revision: int,
        request_id: str,
    ) -> None:
        bounded_text(display_name, 256, 1024, nonempty=True)
        bounded_text(summary, 4096, 4096)
        bounded_text(description, 32768, 32768)
        with self.uow.transaction(request_id) as tx:
            self.authority.project(tx, actor, project_id, MAINTAINERS)
            old = tx.get("package_listings", {"project_id": project_id})
            if old["revision"] != revision:
                raise MarketplaceError("revision_conflict", 409)
            tx.update(
                "package_listings",
                {"project_id": project_id},
                {
                    "display_name": display_name,
                    "summary": summary,
                    "description": description,
                    "revision": revision + 1,
                    "updated_at": now(),
                },
            )
            tx.audit(
                actor.account_id, "listing_changed", str(project_id), {"revision": revision + 1}
            )

    def distribution(
        self,
        actor: Actor,
        project_id: UUID,
        version: str,
        state: str,
        revision: int,
        reason: str,
        request_id: str,
    ) -> None:
        PackageVersion(version)
        bounded_text(reason, 4096, 4096, nonempty=True)
        if state not in {"available", "yanked", "revoked"}:
            raise MarketplaceError("invalid_request")
        with self.uow.transaction(request_id) as tx:
            if actor.scope == "operator":
                self.authority.operator(tx, actor)
                project = tx.get("package_projects", {"id": project_id})
                if state != "revoked":
                    raise MarketplaceError("forbidden", 403)
            else:
                project = self.authority.project(
                    tx,
                    actor,
                    project_id,
                    OWNERS if state == "revoked" else MAINTAINERS,
                    sensitive=state == "revoked",
                )
            release = tx.get(
                "package_releases", {"project_id": project["id"], "exact_version": version}
            )
            if (
                release["publication_state"] != "published"
                or release["distribution_state"] == "revoked"
            ):
                raise MarketplaceError("invalid_state_transition", 409)
            if release["status_revision"] != revision:
                raise MarketplaceError("revision_conflict", 409)
            tx.update(
                "package_releases",
                {"id": release["id"]},
                {
                    "distribution_state": state,
                    "status_revision": revision + 1,
                    "status_updated_at": now(),
                },
            )
            tx.insert(
                "release_status_advisories",
                {
                    "release_id": release["id"],
                    "revision": revision + 1,
                    "state": state,
                    "reason": reason,
                    "created_at": now(),
                },
            )
            tx.audit(
                actor.account_id,
                "release_" + state,
                str(release["id"]),
                {"revision": revision + 1, "reason": reason},
            )
