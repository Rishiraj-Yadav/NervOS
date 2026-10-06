"""Single application authority for G3 package installation, activation, and binding."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Protocol

from nervos_core.application.package_archive import ArchiveValidationProfile, BoundedArchiveReader
from nervos_core.application.package_config_schema import (
    PackageConfigValidationError,
    check_immutable_fields,
    extract_immutable_property_names,
    parse_config_schema_json,
    validate_config,
)
from nervos_core.application.package_environment import SDK_API_VERSION, PackageEnvironmentBuilder
from nervos_core.application.package_integrity import canonical_json_bytes, sha256_hex
from nervos_core.application.package_storage import PackageStore, StagedPackageArtifact
from nervos_core.application.package_verification import (
    CONFIG_SCHEMA_PATH,
    LOCK_PATH,
    MANIFEST_PATH,
    VerifiedPackage,
)
from nervos_core.application.publisher_trust import (
    PublisherTrustService,
    ensure_installable,
)
from nervos_core.domain.agents import AgentInstance
from nervos_core.domain.package_installation import (
    AgentInstancePackageBinding,
    InstalledPackageVersion,
    PackageEnvironment,
    PackageInstallAuthorization,
)
from nervos_core.domain.package_query import (
    ConfigCarryForwardIncompatible,
    PackageHasBoundInstances,
    PackageRemovalOutcome,
    PackageRemovalPlan,
    PackageVersionDetail,
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

    def create_package_instance(
        self,
        *,
        owner_user_id: int,
        package_id: str,
        package_version: str,
        display_name: str,
        model_provider: str,
        model_name: str,
        effective_config_json: str,
        effective_config_digest: str,
        config_schema_digest: str,
        now: datetime,
    ) -> tuple[AgentInstance, AgentInstancePackageBinding]: ...

    def update_instance_config(
        self,
        *,
        owner_user_id: int,
        agent_instance_id: int,
        effective_config_json: str,
        effective_config_digest: str,
        config_schema_digest: str,
        expected_config_revision: int,
        now: datetime,
    ) -> AgentInstancePackageBinding: ...

    def rebind_instance(
        self,
        *,
        owner_user_id: int,
        agent_instance_id: int,
        target_installed_id: int,
        target_package_id: str,
        target_package_version: str,
        effective_config_json: str,
        effective_config_digest: str,
        config_schema_digest: str,
        expected_config_revision: int,
        now: datetime,
    ) -> tuple[AgentInstance, AgentInstancePackageBinding]: ...

    def get_package_detail(self, package_id: str, package_version: str) -> PackageVersionDetail: ...

    def get_installed_id(self, package_id: str, package_version: str) -> int: ...

    def get_binding_for_instance(self, agent_instance_id: int) -> AgentInstancePackageBinding: ...

    def get_package_detail_for_instance(
        self, agent_instance_id: int, owner_user_id: int
    ) -> PackageVersionDetail: ...

    def get_removal_plan(self, package_id: str, package_version: str) -> PackageRemovalPlan: ...

    def mark_pending_removal(self, installed_id: int, now: datetime) -> None: ...

    def finalize_removal(
        self, installed_id: int, now: datetime
    ) -> tuple[str | None, str | None, bool]: ...

    def reconcile_pending_removals(
        self, now: datetime
    ) -> list[tuple[int, str, str, str | None, str | None, bool]]: ...


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
        trust: PublisherTrustService | None = None,
        installation_preflight: Callable[[], None] | None = None,
    ) -> None:
        self._registry = registry
        self._store = store
        self._environment_builder = environment_builder
        self._health_checker = health_checker
        self._clock = clock
        # Stage H5 (ADR 0036). Absent means "no local revocation state", which is the
        # Stage-G behaviour exactly; the API composition always supplies it.
        self._trust = trust
        self._installation_preflight = installation_preflight

    def install(
        self,
        source: Path | BinaryIO,
        authorization: PackageInstallAuthorization,
    ) -> PackageInstallResult:
        staged = self._store.snapshot_and_verify(source)
        installed_id: int | None = None
        try:
            self._require_authorization(staged, authorization)
            if self._trust is not None:
                ensure_installable(self._trust, staged.verified.signer_fingerprint)
            if self._installation_preflight is not None:
                self._installation_preflight()
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
                sdk_version=SDK_API_VERSION,
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
                expected_sdk_api_version=SDK_API_VERSION,
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

    def create_package_instance(
        self,
        *,
        owner_user_id: int,
        package_id: str,
        package_version: str,
        display_name: str,
        model_provider: str,
        model_name: str,
        config: Mapping[str, object],
    ) -> tuple[AgentInstance, AgentInstancePackageBinding]:
        detail = self._registry.get_package_detail(package_id, package_version)
        if detail.status.value != "active":
            raise ValueError(f"Package {package_id}@{package_version} is not active.")
        schema = parse_config_schema_json(canonical_json_bytes(detail.config_schema))
        effective = validate_config(schema, config)
        payload = canonical_json_bytes(effective)
        schema_bytes = canonical_json_bytes(detail.config_schema)
        return self._registry.create_package_instance(
            owner_user_id=owner_user_id,
            package_id=package_id,
            package_version=package_version,
            display_name=display_name,
            model_provider=model_provider,
            model_name=model_name,
            effective_config_json=payload.decode("utf-8"),
            effective_config_digest=sha256_hex(payload),
            config_schema_digest=sha256_hex(schema_bytes),
            now=self._clock(),
        )

    def update_instance_config(
        self,
        *,
        owner_user_id: int,
        agent_instance_id: int,
        config: Mapping[str, object],
        expected_config_revision: int,
    ) -> AgentInstancePackageBinding:
        detail = self._registry.get_package_detail_for_instance(agent_instance_id, owner_user_id)
        current_binding = self._registry.get_binding_for_instance(agent_instance_id)
        schema = parse_config_schema_json(canonical_json_bytes(detail.config_schema))
        effective = validate_config(schema, config)

        immutable_fields = extract_immutable_property_names(schema)
        current_config = json.loads(current_binding.effective_config_json)
        check_immutable_fields(current_config, effective, immutable_fields)

        payload = canonical_json_bytes(effective)
        schema_bytes = canonical_json_bytes(detail.config_schema)
        return self._registry.update_instance_config(
            owner_user_id=owner_user_id,
            agent_instance_id=agent_instance_id,
            effective_config_json=payload.decode("utf-8"),
            effective_config_digest=sha256_hex(payload),
            config_schema_digest=sha256_hex(schema_bytes),
            expected_config_revision=expected_config_revision,
            now=self._clock(),
        )

    def rebind_instance(
        self,
        *,
        owner_user_id: int,
        agent_instance_id: int,
        target_package_version: str,
        config: Mapping[str, object] | None,
        expected_config_revision: int,
    ) -> tuple[AgentInstance, AgentInstancePackageBinding]:
        current_detail = self._registry.get_package_detail_for_instance(
            agent_instance_id, owner_user_id
        )
        current_binding = self._registry.get_binding_for_instance(agent_instance_id)
        target_detail = self._registry.get_package_detail(
            current_detail.package_id, target_package_version
        )
        if self._trust is not None:
            ensure_installable(self._trust, target_detail.signer_fingerprint)
        if target_detail.status.value != "active":
            target_id = current_detail.package_id
            raise ValueError(f"Target {target_id}@{target_package_version} is not active.")

        target_schema = parse_config_schema_json(canonical_json_bytes(target_detail.config_schema))

        if config is not None:
            effective = validate_config(target_schema, config)
        else:
            current_config = json.loads(current_binding.effective_config_json)
            try:
                effective = validate_config(target_schema, current_config)
            except PackageConfigValidationError as err:
                raise ConfigCarryForwardIncompatible(
                    f"Current configuration is incompatible with {target_package_version}: {err}"
                ) from err

        immutable_fields = extract_immutable_property_names(target_schema)
        current_config = json.loads(current_binding.effective_config_json)
        check_immutable_fields(current_config, effective, immutable_fields)

        payload = canonical_json_bytes(effective)
        schema_bytes = canonical_json_bytes(target_detail.config_schema)
        target_installed_id = self._registry.get_installed_id(
            target_detail.package_id, target_package_version
        )
        return self._registry.rebind_instance(
            owner_user_id=owner_user_id,
            agent_instance_id=agent_instance_id,
            target_installed_id=target_installed_id,
            target_package_id=target_detail.package_id,
            target_package_version=target_package_version,
            effective_config_json=payload.decode("utf-8"),
            effective_config_digest=sha256_hex(payload),
            config_schema_digest=sha256_hex(schema_bytes),
            expected_config_revision=expected_config_revision,
            now=self._clock(),
        )

    def remove_package(self, package_id: str, package_version: str) -> PackageRemovalOutcome:
        plan = self._registry.get_removal_plan(package_id, package_version)
        if plan.bound_instances_count > 0:
            msg = (
                f"Cannot remove package {package_id}@{package_version}: "
                f"{plan.bound_instances_count} bound instances."
            )
            raise PackageHasBoundInstances(msg)
        installed_id = self._registry.get_installed_id(package_id, package_version)
        if plan.nonterminal_runs_count > 0:
            self._registry.mark_pending_removal(installed_id, self._clock())
            return PackageRemovalOutcome.PENDING_REMOVAL

        storage_key, env_key, is_env_unshared = self._registry.finalize_removal(
            installed_id, self._clock()
        )
        if storage_key:
            self._store.remove_payload(storage_key)
        if env_key and is_env_unshared:
            self._store.remove_environment(env_key)
        return PackageRemovalOutcome.REMOVED

    def reconcile_pending_removals(self) -> int:
        """Find pending removals with 0 obligations and finalize physical and DB cleanup."""
        ready_items = self._registry.reconcile_pending_removals(self._clock())
        for _, _, _, storage_key, env_key, is_env_unshared in ready_items:
            if storage_key:
                self._store.remove_payload(storage_key)
            if env_key and is_env_unshared:
                self._store.remove_environment(env_key)
        return len(ready_items)

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
