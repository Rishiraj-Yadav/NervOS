"""MVP exact Marketplace download and Stage-G installation handoff."""

from __future__ import annotations

import hashlib
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from nervos_api.application.marketplace_discovery import MarketplaceDiscoveryService
from nervos_core.application.errors import PersistenceUnavailable
from nervos_core.application.package_installation import PackageApplicationService
from nervos_core.application.package_query import PackageQueryService
from nervos_core.domain.package_installation import (
    PackageInstallAuthorization,
    PackageInstallStatus,
    validate_sha256,
)
from nervos_core.domain.packages import PackageVersion, validate_package_id
from sqlalchemy import text
from sqlalchemy.engine import Engine
from starlette.concurrency import run_in_threadpool


class MarketplaceRequestNotFound(Exception):
    """The requested ticket is absent or belongs to another owner."""


class MarketplaceRequestConflict(Exception):
    """The retained ticket cannot perform this transition."""


class MarketplaceInstallService:
    def __init__(
        self,
        engine: Engine,
        discovery: MarketplaceDiscoveryService,
        packages: PackageApplicationService,
        temporary_root: Path,
        clock: Any,
        query: PackageQueryService,
    ) -> None:
        self.engine, self.discovery, self.packages = engine, discovery, packages
        self.root, self.clock = temporary_root, clock
        self.query = query

    def cleanup_expired(self) -> None:
        """Expire only idle tickets; G owns any installation already handed off."""
        with self.engine.begin() as connection:
            rows = connection.execute(
                text(
                    "UPDATE marketplace_install_requests SET state='failed',"
                    "error_code='request_expired',artifact_path=NULL,updated_at=:now "
                    "WHERE state IN ('created','downloaded') AND created_at<:cutoff "
                    "RETURNING id"
                ),
                {"now": self.clock(), "cutoff": self.clock() - timedelta(hours=1)},
            ).all()
        # Remove old files only after their durable references are withdrawn.
        if rows:
            self._cleanup_unreferenced()

    def _cleanup_unreferenced(self) -> None:
        with self.engine.connect() as connection:
            referenced = set(
                connection.execute(
                    text(
                        "SELECT artifact_path FROM marketplace_install_requests "
                        "WHERE artifact_path IS NOT NULL"
                    )
                ).scalars()
            )
        if self.root.exists():
            for path in self.root.glob("*.nervos"):
                # A just-downloaded file has not yet committed its reference. Leave it alone.
                if (
                    str(path) not in referenced
                    and path.stat().st_mtime < self.clock().timestamp() - 3600
                ):
                    path.unlink(missing_ok=True)

    def _row(self, owner: int, request_id: int) -> dict[str, Any]:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT * FROM marketplace_install_requests "
                        "WHERE id=:id AND owner_user_id=:owner"
                    ),
                    {"id": request_id, "owner": owner},
                )
                .mappings()
                .first()
            )
        if row is None:
            raise MarketplaceRequestNotFound
        return dict(row)

    async def create(
        self,
        owner: int,
        origin: str,
        package_id: str,
        version: str,
        acknowledgement: str | None = None,
    ) -> dict[str, Any]:
        self.cleanup_expired()
        release = await self.discovery.release(package_id, version)
        try:
            validate_package_id(package_id)
            PackageVersion(version)
            for key in ("archive_sha256", "content_digest", "signer_fingerprint"):
                validate_sha256(release[key])
            if not isinstance(release["status_revision"], int) or release["status_revision"] <= 0:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise MarketplaceRequestConflict from None
        if (
            release.get("package_id") != package_id
            or release.get("exact_version") != version
            or not (
                release.get("distribution_state") == "available"
                or (
                    release.get("distribution_state") == "yanked"
                    and acknowledgement == release.get("archive_sha256")
                )
            )
            or not isinstance(release.get("size_bytes"), int)
            or not 0 < release["size_bytes"] <= 256 * 1024 * 1024
        ):
            raise MarketplaceRequestConflict
        now = self.clock()
        with self.engine.begin() as connection:
            request_id = connection.execute(
                text(
                    "INSERT INTO marketplace_install_requests "
                    "(owner_user_id,marketplace_origin,package_id,package_version,"
                    "expected_archive_sha256,expected_content_digest,expected_signer_fingerprint,"
                    "observed_status_revision,observed_distribution_state,expected_size_bytes,"
                    "state,created_at,updated_at) "
                    "SELECT :owner,:origin,:package,:version,:archive,:content,:signer,"
                    ":revision,:distribution,:size,'created',:now,:now "
                    "WHERE (SELECT COUNT(*) FROM marketplace_install_requests "
                    "WHERE state IN ('created','downloaded','approved')) < 16 "
                    "AND (SELECT COALESCE(SUM(expected_size_bytes),0) "
                    "FROM marketplace_install_requests "
                    "WHERE state IN ('created','downloaded','approved')) + :size <= 536870912 "
                    "RETURNING id"
                ),
                {
                    "owner": owner,
                    "origin": origin,
                    "package": package_id,
                    "version": version,
                    "archive": release["archive_sha256"],
                    "content": release["content_digest"],
                    "signer": release["signer_fingerprint"],
                    "revision": release["status_revision"],
                    "size": release["size_bytes"],
                    "distribution": release["distribution_state"],
                    "now": now,
                },
            ).scalar_one_or_none()
        if request_id is None:
            raise MarketplaceRequestConflict
        return self.status(owner, int(request_id))

    def status(self, owner: int, request_id: int) -> dict[str, Any]:
        row = self._row(owner, request_id)
        row.pop("artifact_path", None)
        row.pop("owner_user_id", None)
        return row

    def cancel(self, owner: int, request_id: int) -> dict[str, Any]:
        row = self._row(owner, request_id)
        with self.engine.begin() as connection:
            changed = connection.execute(
                text(
                    "UPDATE marketplace_install_requests SET state='failed',"
                    "error_code='request_cancelled',artifact_path=NULL,updated_at=:now "
                    "WHERE id=:id AND state IN ('created','downloaded','failed')"
                ),
                {"id": request_id, "now": self.clock()},
            ).rowcount
        if changed != 1:
            raise MarketplaceRequestConflict
        if row["artifact_path"]:
            Path(row["artifact_path"]).unlink(missing_ok=True)
        return self.status(owner, request_id)

    def _require_unexpired(self, row: dict[str, Any]) -> None:
        created = datetime.fromisoformat(str(row["created_at"]))
        current = self.clock()
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        if current > created + timedelta(hours=1):
            self._mark_failed(row["id"], "request_expired", row["state"])
            if row["artifact_path"]:
                Path(row["artifact_path"]).unlink(missing_ok=True)
            raise MarketplaceRequestConflict

    async def prepare(self, owner: int, request_id: int) -> dict[str, Any]:
        row = self._row(owner, request_id)
        self._require_unexpired(row)
        if row["state"] == "downloaded":
            return self.status(owner, request_id)
        if row["state"] != "created" or row["marketplace_origin"] != self.discovery.origin:
            raise MarketplaceRequestConflict
        try:
            body = await self.discovery.download(
                row["package_id"],
                row["package_version"],
                max_bytes=row["expected_size_bytes"],
                acknowledgement=(
                    row["expected_archive_sha256"]
                    if row["observed_distribution_state"] == "yanked"
                    else None
                ),
            )
        except PersistenceUnavailable:
            self._mark_failed(request_id, "download_unavailable")
            raise
        if len(body) != row["expected_size_bytes"]:
            self._mark_failed(request_id, "archive_size_mismatch")
            raise MarketplaceRequestConflict
        digest = hashlib.sha256(body).hexdigest()
        if digest != row["expected_archive_sha256"]:
            self._mark_failed(request_id, "archive_digest_mismatch")
            raise MarketplaceRequestConflict
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=self.root, suffix=".nervos", delete=False) as file:
            file.write(body)
            path = Path(file.name)
        try:
            verified = await run_in_threadpool(self.query.inspect_artifact, path)
            if (
                verified.package_id != row["package_id"]
                or verified.package_version != row["package_version"]
                or verified.signer_fingerprint != row["expected_signer_fingerprint"]
                or verified.content_digest != row["expected_content_digest"]
                or verified.archive_digest != row["expected_archive_sha256"]
                or not verified.is_compatible
            ):
                raise MarketplaceRequestConflict
        except Exception:
            path.unlink(missing_ok=True)
            self._mark_failed(request_id, "local_verification_failed")
            raise
        with self.engine.begin() as connection:
            changed = connection.execute(
                text(
                    "UPDATE marketplace_install_requests SET state='downloaded',"
                    "artifact_path=:path,updated_at=:now WHERE id=:id AND state='created'"
                ),
                {"id": request_id, "path": str(path), "now": self.clock()},
            ).rowcount
        if changed != 1:
            path.unlink(missing_ok=True)
            raise MarketplaceRequestConflict
        return self.status(owner, request_id)

    async def install(self, owner: int, request_id: int) -> dict[str, Any]:
        row = self._row(owner, request_id)
        if row["state"] == "installed":
            return self.status(owner, request_id)
        if row["state"] == "approved":
            # Reconcile a crash after G committed ACTIVE, without redispatching installation.
            installed = self.query.get_package_detail(row["package_id"], row["package_version"])
            if (
                installed.status != PackageInstallStatus.ACTIVE
                or installed.archive_digest != row["expected_archive_sha256"]
                or installed.content_digest != row["expected_content_digest"]
                or installed.signer_fingerprint != row["expected_signer_fingerprint"]
            ):
                raise MarketplaceRequestConflict
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE marketplace_install_requests SET state='installed',"
                        "artifact_path=NULL,updated_at=:now WHERE id=:id AND state='approved'"
                    ),
                    {"id": request_id, "now": self.clock()},
                )
            if row["artifact_path"]:
                Path(row["artifact_path"]).unlink(missing_ok=True)
            return self.status(owner, request_id)
        self._require_unexpired(row)
        if row["state"] != "downloaded" or not row["artifact_path"]:
            raise MarketplaceRequestConflict
        path = Path(row["artifact_path"])
        if row["marketplace_origin"] != self.discovery.origin:
            raise MarketplaceRequestConflict
        current = await self.discovery.release(row["package_id"], row["package_version"])
        if (
            current.get("distribution_state") != row["observed_distribution_state"]
            or current.get("status_revision") != row["observed_status_revision"]
            or current.get("archive_sha256") != row["expected_archive_sha256"]
            or current.get("content_digest") != row["expected_content_digest"]
            or current.get("signer_fingerprint") != row["expected_signer_fingerprint"]
        ):
            self._mark_failed(request_id, "release_status_changed", "downloaded")
            path.unlink(missing_ok=True)
            raise MarketplaceRequestConflict
        with self.engine.begin() as connection:
            changed = connection.execute(
                text(
                    "UPDATE marketplace_install_requests SET state='approved' "
                    "WHERE id=:id AND state='downloaded'"
                ),
                {"id": request_id},
            ).rowcount
        if changed != 1:
            raise MarketplaceRequestConflict
        try:
            await run_in_threadpool(
                self.packages.install,
                path,
                PackageInstallAuthorization(
                    package_id=row["package_id"],
                    package_version=row["package_version"],
                    content_digest=row["expected_content_digest"],
                    signer_fingerprint=row["expected_signer_fingerprint"],
                    archive_digest=row["expected_archive_sha256"],
                    approved_by_user_id=owner,
                    approved_at=self.clock(),
                ),
            )
        except Exception:
            path.unlink(missing_ok=True)
            self._mark_failed(request_id, "local_verification_failed", "approved")
            raise
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE marketplace_install_requests SET state='installed',"
                    "artifact_path=NULL,updated_at=:now WHERE id=:id"
                ),
                {"id": request_id, "now": self.clock()},
            )
        path.unlink(missing_ok=True)
        return self.status(owner, request_id)

    def _mark_failed(self, request_id: int, code: str, expected_state: str = "created") -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE marketplace_install_requests SET state='failed',"
                    "error_code=:code,updated_at=:now WHERE id=:id AND state=:expected_state"
                ),
                {
                    "id": request_id,
                    "code": code,
                    "now": self.clock(),
                    "expected_state": expected_state,
                },
            )
