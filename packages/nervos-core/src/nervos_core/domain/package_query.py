"""Domain types for package inspection, query summaries, and removal plans."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from nervos_core.domain.package_installation import PackageEnvironmentStatus, PackageInstallStatus


class PackageRemovalRefused(ValueError):
    """Raised when a package cannot be removed due to durable obligations."""


class PackageHasBoundInstances(PackageRemovalRefused):
    """The package cannot be removed because one or more AgentInstances are bound to it."""


class PackageHasActiveRuns(PackageRemovalRefused):
    """The package cannot be immediately removed because nonterminal runs are executing."""


class StaleConfigRevision(ValueError):
    """The configuration was concurrently modified and the expected revision is stale."""


class ImmutableConfigViolation(ValueError):
    """An immutable configuration property was modified."""


class ConfigCarryForwardIncompatible(ValueError):
    """The existing effective configuration is incompatible with the target package schema."""


class IncompatiblePackageVersion(ValueError):
    """The package is not compatible with this version of the NervOS runtime."""


class PackageRemovalOutcome(StrEnum):
    REMOVED = "removed"
    PENDING_REMOVAL = "pending_removal"
    REFUSED_HAS_INSTANCES = "refused_has_instances"
    REFUSED_HAS_RUNS = "refused_has_runs"


@dataclass(frozen=True, slots=True)
class InspectedPackageMetadata:
    """Safe verified metadata returned from read-only package inspection."""

    package_id: str
    package_version: str
    display_name: str
    manifest_version: str
    signer_fingerprint: str
    content_digest: str
    archive_digest: str
    min_nervos_version: str
    max_nervos_version: str | None
    is_compatible: bool
    entrypoint_module: str
    entrypoint_object: str
    tools_required: tuple[str, ...]
    tools_optional: tuple[str, ...]
    memory_declarations: tuple[str, ...]
    trigger_declarations: tuple[str, ...]
    config_schema: dict[str, object]
    resource_limits: dict[str, int]


@dataclass(frozen=True, slots=True)
class PackageVersionSummary:
    """Summary of one installed package release for lists and cards."""

    package_id: str
    package_version: str
    display_name: str
    status: PackageInstallStatus
    signer_fingerprint: str
    content_digest: str
    archive_digest: str
    bound_instances_count: int
    installed_at: datetime | None
    activated_at: datetime | None
    failed_at: datetime | None
    removed_at: datetime | None
    last_error_code: str | None
    last_error_message: str | None


@dataclass(frozen=True, slots=True)
class PackageVersionDetail:
    """Full details of one installed package release."""

    package_id: str
    package_version: str
    display_name: str
    status: PackageInstallStatus
    signer_fingerprint: str
    content_digest: str
    archive_digest: str
    manifest_version: str
    min_nervos_version: str
    max_nervos_version: str | None
    is_compatible: bool
    entrypoint_module: str
    entrypoint_object: str
    tools_required: tuple[str, ...]
    tools_optional: tuple[str, ...]
    memory_declarations: tuple[str, ...]
    trigger_declarations: tuple[str, ...]
    config_schema: dict[str, object]
    resource_limits: dict[str, int]
    bound_instances_count: int
    environment_id: int | None
    environment_status: PackageEnvironmentStatus | None
    installed_at: datetime | None
    activated_at: datetime | None
    failed_at: datetime | None
    removed_at: datetime | None
    last_error_code: str | None
    last_error_message: str | None


@dataclass(frozen=True, slots=True)
class PackageRemovalPlan:
    """Pre-removal assessment of durable blockers and environment sharing."""

    package_id: str
    package_version: str
    status: PackageInstallStatus
    bound_instance_ids: tuple[int, ...]
    bound_instances_count: int
    nonterminal_run_ids: tuple[int, ...]
    nonterminal_runs_count: int
    is_environment_shared: bool
    can_remove_immediately: bool
    can_begin_removal: bool
    blocking_reasons: tuple[str, ...]
