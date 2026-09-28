"""Single application authority for G3 package installation, activation, and binding."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Protocol

from nervos_core.application.package_archive import ArchiveValidationProfile, BoundedArchiveReader
from nervos_core.application.package_config_schema import parse_config_schema_json, validate_config
from nervos_core.application.package_environment import PackageEnvironmentBuilder
from nervos_core.application.package_integrity import canonical_json_bytes, sha256_hex
from nervos_core.application.package_storage import PackageStore, StagedPackageArtifact
from nervos_core.application.package_verification import (
    CONFIG_SCHEMA_PATH,
    LOCK_PATH,
    MANIFEST_PATH,
    VerifiedPackage,
)
from nervos_core.domain.package_installation import (
    AgentInstancePackageBinding,
    InstalledPackageVersion,
    PackageEnvironment,
    PackageInstallAuthorization,
)


class PackageAuthorizationMismatch(ValueError):
    """The operator authorization does not bind the staged verified artifact."""


class PackageHealthCheckFailed(ValueError):
    """The environment/host/entrypoint health check failed before activation."""


class PackageHealthChecker(Protocol):
    def check(
        self,
        *,
        environment: Path,
        entrypoint: str,
        expected_sdk_api_version: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class PackageInstallRecordInput:
    """Verified evidence that the registry persists in one short transaction."""

    verified: VerifiedPackage
    manifest_bytes: bytes
    config_schema_bytes: bytes
    dependency_lock_bytes: bytes
    staging_key: str
    authorization: PackageInstallAuthorization
    now: datetime


class PackageRegistryPersistence(Protocol):
    """Application-owned registry port; SQLAlchemy is an infrastructure implementation."""

    def begin_install(self, command: PackageInstallRecordInput) -> InstalledPackageVersion: ...

    def mark_installed(self, installed_id: int, storage_key: str, now: datetime) -> None: ...

    def create_or_get_environment(
        self,
        *,
        environment_digest: str,
        environment_key_json: str,
        environment_key: str,
        sdk_version: str,
        sdk_wheel_digest: str,
        host_version: str,
        host_wheel_digest: str,
        now: datetime,
    ) -> PackageEnvironment: ...

    def mark_environment_ready(self, environment_id: int, now: datetime) -> None: ...

    def attach_environment(self, installed_id: int, environment_id: int, now: datetime) -> None: ...

    def mark_active(self, installed_id: int, now: datetime) -> InstalledPackageVersion: ...

    def mark_failed(self, installed_id: int, code: str, message: str, now: datetime) -> None: ...

    def load_verified_bytes(self, installed_id: int) -> tuple[bytes, bytes, bytes]: ...

    def bind_instance(
        self,
        *,
        owner_user_id: int,
        agent_instance_id: int,
        installed_id: int,
        effective_config_json: str,
        effective_config_digest: str,
        config_schema_digest: str,
        now: datetime,
    ) -> AgentInstancePackageBinding: ...


@dataclass(frozen=True, slots=True)
class PackageInstallResult:
    package: InstalledPackageVersion
    staged_archive_digest: str


class PackageApplicationService:
    """Own every package lifecycle mutation; callers never manipulate package DB/files directly."""

    def __init__(
        self,
        registry: PackageRegistryPersistence,
        store: PackageStore,
        environment_builder: PackageEnvironmentBuilder,
        health_checker: PackageHealthChecker,
        clock: Callable[[], datetime],
    ) -> None:
        self._registry = registry
        self._store = store
        self._environment_builder = environment_builder
        self._health_checker = health_checker
        self._clock = clock

    def install(
        self,
        source: Path | BinaryIO,
        authorization: PackageInstallAuthorization,
    ) -> PackageInstallResult:
        staged = self._store.snapshot_and_verify(source)
        installed_id: int | None = None
        try:
            self._require_authorization(staged, authorization)
            reader = BoundedArchiveReader(
                staged.archive, profile=ArchiveValidationProfile.NERVOS_V1
            )
            command = PackageInstallRecordInput(
                verified=staged.verified,
                manifest_bytes=reader.read(MANIFEST_PATH),
                config_schema_bytes=reader.read(CONFIG_SCHEMA_PATH),
                dependency_lock_bytes=reader.read(LOCK_PATH),
                staging_key=f"staging/{staged.operation_id}",
                authorization=authorization,
                now=self._clock(),
            )
            installed = self._registry.begin_install(command)
            installed_id = installed.id
            if installed.status.value in {"installed", "active"}:
                return PackageInstallResult(installed, staged.archive_digest)
            payload = self._store.materialize(staged)
            storage_key, payload_path = self._store.publish_payload(staged, payload)
            self._registry.mark_installed(installed.id, storage_key, self._clock())
            identity, environment_path = self._environment_builder.build(
                staged.verified, payload_path
            )
            environment = self._registry.create_or_get_environment(
                environment_digest=identity.digest,
                environment_key_json=identity.canonical_json,
                environment_key=identity.relative_key,
                sdk_version=self._environment_builder.runtime.sdk.version,
                sdk_wheel_digest=self._environment_builder.runtime.sdk.sha256,
                host_version=self._environment_builder.runtime.host.version,
                host_wheel_digest=self._environment_builder.runtime.host.sha256,
                now=self._clock(),
            )
            self._registry.mark_environment_ready(environment.id, self._clock())
            self._registry.attach_environment(installed.id, environment.id, self._clock())
            self._health_checker.check(
                environment=environment_path,
                entrypoint=staged.verified.manifest.runtime.entrypoint,
                expected_sdk_api_version=self._environment_builder.runtime.sdk.version,
            )
            active = self._registry.mark_active(installed.id, self._clock())
            return PackageInstallResult(active, staged.archive_digest)
        except BaseException:
            if installed_id is not None:
                self._registry.mark_failed(
                    installed_id,
                    "package_install_failed",
                    "The package installation failed safely.",
                    self._clock(),
                )
            raise
        finally:
            self._store.cleanup_staging(staged)

    def bind_instance(
        self,
        *,
        owner_user_id: int,
        agent_instance_id: int,
        installed_package_version_id: int,
        config: Mapping[str, object],
    ) -> AgentInstancePackageBinding:
        _, schema_bytes, _ = self._registry.load_verified_bytes(installed_package_version_id)
        schema = parse_config_schema_json(schema_bytes)
        effective = validate_config(schema, config)
        payload = canonical_json_bytes(effective)
        return self._registry.bind_instance(
            owner_user_id=owner_user_id,
            agent_instance_id=agent_instance_id,
            installed_id=installed_package_version_id,
            effective_config_json=payload.decode("utf-8"),
            effective_config_digest=sha256_hex(payload),
            config_schema_digest=sha256_hex(schema_bytes),
            now=self._clock(),
        )

    @staticmethod
    def _require_authorization(
        staged: StagedPackageArtifact, authorization: PackageInstallAuthorization
    ) -> None:
        verified = staged.verified
        if (
            authorization.package_id != verified.manifest.package_id
            or authorization.package_version != verified.manifest.package_version
            or authorization.content_digest != verified.content_digest
            or authorization.signer_fingerprint != verified.signer_fingerprint
            or (
                authorization.archive_digest is not None
                and authorization.archive_digest != staged.archive_digest
            )
        ):
            raise PackageAuthorizationMismatch
