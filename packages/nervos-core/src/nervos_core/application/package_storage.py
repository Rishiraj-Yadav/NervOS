"""Same-volume immutable artifact snapshots and safe package payload materialization."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from nervos_core.application.package_archive import (
    ARCHIVE_MAX_BYTES,
    READ_CHUNK_BYTES,
    ArchiveValidationProfile,
    BoundedArchiveReader,
)
from nervos_core.application.package_paths import canonical_package_path
from nervos_core.application.package_verification import VerifiedPackage, verify_package


class PackageStorageError(ValueError):
    """A package artifact could not be snapshotted or materialized safely."""


@dataclass(frozen=True, slots=True)
class StagedPackageArtifact:
    operation_id: str
    directory: Path
    archive: Path
    archive_digest: str
    size: int
    verified: VerifiedPackage


class PackageStore:
    """Own same-volume staging, immutable payload publication, and reconciliation paths."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve(strict=False)
        self.staging_root = self.root / "staging"
        self.packages_root = self.root / "packages"
        self.environments_root = self.root / "environments"

    def prepare(self) -> None:
        for path in (self.staging_root, self.packages_root, self.environments_root):
            path.mkdir(parents=True, exist_ok=True)

    def snapshot_and_verify(self, source: Path | BinaryIO) -> StagedPackageArtifact:
        """Copy a caller-owned source once, then verify only the immutable staged snapshot."""
        self.prepare()
        directory = Path(tempfile.mkdtemp(prefix="install-", dir=self.staging_root))
        archive = directory / "artifact.nervos"
        digest = hashlib.sha256()
        total = 0
        try:
            with archive.open("xb") as destination:
                if isinstance(source, Path):
                    source_handle = source.open("rb")
                    close_source = True
                else:
                    source_handle = source
                    close_source = False
                try:
                    while chunk := source_handle.read(READ_CHUNK_BYTES):
                        total += len(chunk)
                        if total > ARCHIVE_MAX_BYTES:
                            raise PackageStorageError(
                                "package artifact exceeds the G2 archive limit"
                            )
                        digest.update(chunk)
                        destination.write(chunk)
                    destination.flush()
                    os.fsync(destination.fileno())
                finally:
                    if close_source:
                        source_handle.close()
            verified = verify_package(archive)
            archive_digest = digest.hexdigest()
            if verified.archive_digest != archive_digest:
                raise PackageStorageError(
                    "staged package archive digest changed during verification"
                )
            return StagedPackageArtifact(
                operation_id=directory.name,
                directory=directory,
                archive=archive,
                archive_digest=archive_digest,
                size=total,
                verified=verified,
            )
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    def materialize(self, staged: StagedPackageArtifact) -> Path:
        """Stream only G2-approved members under a private payload directory and rehash them."""
        payload_root = staged.directory / "payload"
        payload_root.mkdir(exist_ok=False)
        reader = BoundedArchiveReader(staged.archive, profile=ArchiveValidationProfile.NERVOS_V1)
        expected = {entry.path: entry for entry in staged.verified.entries}
        for path in sorted(expected, key=lambda item: item.encode("utf-8")):
            canonical_package_path(path)
            destination = (payload_root / Path(*path.split("/"))).resolve(strict=False)
            if payload_root != destination and payload_root not in destination.parents:
                raise PackageStorageError("package member escapes payload staging root")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise PackageStorageError("package materialization refuses overwrite")
            digest = hashlib.sha256()
            size = 0
            with destination.open("xb") as handle:
                for chunk in reader.stream(path):
                    size += len(chunk)
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            entry = expected[path]
            if size != entry.size or digest.hexdigest() != entry.sha256:
                raise PackageStorageError("materialized package payload failed G2 integrity check")
        return payload_root

    def package_storage_key(self, verified: VerifiedPackage) -> str:
        package_component = verified.manifest.package_id.replace(".", "_")
        version_component = verified.manifest.package_version.replace("+", "_")
        return f"packages/{package_component}/{version_component}/{verified.content_digest}"

    def publish_payload(
        self, staged: StagedPackageArtifact, payload_root: Path
    ) -> tuple[str, Path]:
        key = self.package_storage_key(staged.verified)
        destination = self.root / Path(*key.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            self.verify_materialized(destination, staged.verified)
            shutil.rmtree(payload_root, ignore_errors=True)
            return key, destination
        try:
            os.replace(payload_root, destination)
        except OSError as error:
            raise PackageStorageError(
                "package payload could not be published atomically"
            ) from error
        return key, destination

    def verify_materialized(self, root: Path, verified: VerifiedPackage) -> None:
        expected = {entry.path: entry for entry in verified.entries}
        actual: set[str] = set()
        for path in root.rglob("*"):
            if path.is_file():
                actual.add(path.relative_to(root).as_posix())
        if actual != set(expected):
            raise PackageStorageError("installed payload inventory does not match verified package")
        for path, entry in expected.items():
            target = root / Path(*path.split("/"))
            digest = hashlib.sha256()
            size = 0
            with target.open("rb") as handle:
                while chunk := handle.read(READ_CHUNK_BYTES):
                    digest.update(chunk)
                    size += len(chunk)
            if size != entry.size or digest.hexdigest() != entry.sha256:
                raise PackageStorageError("installed payload digest mismatch")

    def cleanup_staging(self, staged: StagedPackageArtifact) -> None:
        shutil.rmtree(staged.directory, ignore_errors=True)

    def remove_payload(self, storage_key: str) -> bool:
        """Safely delete the published payload directory if it exists."""
        target = self.root / Path(*storage_key.split("/"))
        if target.exists() and target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            # Safely prune empty version/package directories up to packages_root
            try:
                parent = target.parent
                while parent != self.packages_root and parent.is_dir():
                    if not any(parent.iterdir()):
                        parent.rmdir()
                        parent = parent.parent
                    else:
                        break
            except OSError:
                pass
            return True
        return False

    def remove_environment(self, environment_key: str) -> bool:
        """Safely delete an environment directory if it exists."""
        target = self.root / Path(*environment_key.split("/"))
        if target.exists() and target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            return True
        return False

    def reconcile_orphan_staging(self, durable_operation_ids: frozenset[str]) -> tuple[str, ...]:
        """Controlled startup cleanup; callers must run this before accepting new installs."""
        if not self.staging_root.exists():
            return ()
        removed: list[str] = []
        for path in sorted(self.staging_root.iterdir(), key=lambda item: item.name):
            if path.is_dir() and path.name not in durable_operation_ids:
                shutil.rmtree(path, ignore_errors=False)
                removed.append(path.name)
        return tuple(removed)
