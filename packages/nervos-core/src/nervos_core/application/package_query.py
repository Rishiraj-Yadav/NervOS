"""Read-only query service for package inspection, listings, and removal plans."""

from __future__ import annotations

import json
from pathlib import Path
from typing import BinaryIO, Protocol, cast

from nervos_core.application.package_archive import ArchiveValidationProfile, BoundedArchiveReader
from nervos_core.application.package_manifest import parse_package_manifest
from nervos_core.application.package_storage import PackageStore
from nervos_core.application.package_verification import (
    CONFIG_SCHEMA_PATH,
    MANIFEST_PATH,
)
from nervos_core.domain.package_installation import PackageInstallStatus
from nervos_core.domain.package_query import (
    InspectedPackageMetadata,
    PackageRemovalPlan,
    PackageVersionDetail,
    PackageVersionSummary,
)
from nervos_core.domain.packages import NERVOS_CORE_COMPATIBILITY_VERSION, PackageVersion


class PackageQueryPersistence(Protocol):
    """Query port for package releases and removal blockers."""

    def list_package_summaries(
        self, status: PackageInstallStatus | None = None
    ) -> tuple[PackageVersionSummary, ...]: ...

    def get_package_detail(self, package_id: str, package_version: str) -> PackageVersionDetail: ...

    def get_removal_plan(self, package_id: str, package_version: str) -> PackageRemovalPlan: ...


class PackageQueryService:
    """Read-only package query authority for API, CLI, and UI surfaces."""

    def __init__(
        self,
        persistence: PackageQueryPersistence,
        store: PackageStore,
    ) -> None:
        self._persistence = persistence
        self._store = store

    def inspect_artifact(self, source: Path | BinaryIO) -> InspectedPackageMetadata:
        """Inspect a .nervos archive safely without modifying DB state or executing code."""
        staged = self._store.snapshot_and_verify(source)
        try:
            reader = BoundedArchiveReader(
                staged.archive, profile=ArchiveValidationProfile.NERVOS_V1
            )
            manifest_bytes = reader.read(MANIFEST_PATH)
            config_schema_bytes = reader.read(CONFIG_SCHEMA_PATH)
            manifest = parse_package_manifest(manifest_bytes)
            parsed_schema: object = json.loads(config_schema_bytes.decode("utf-8"))
            schema_json: dict[str, object] = (
                {str(k): v for k, v in cast("dict[object, object]", parsed_schema).items()}
                if isinstance(parsed_schema, dict)
                else {}
            )

            min_ver = manifest.nervos.min_version.value
            max_ver = manifest.nervos.max_version.value
            curr = PackageVersion(NERVOS_CORE_COMPATIBILITY_VERSION)
            is_compatible = manifest.nervos.min_version <= curr <= manifest.nervos.max_version

            tools_required = tuple(manifest.tools.required)
            tools_optional = tuple(manifest.tools.optional)
            memory_decls: list[str] = []
            if manifest.memory.reads:
                memory_decls.append("reads")
            if manifest.memory.writes:
                memory_decls.append("writes")
            trigger_decls = tuple(k.value for k in manifest.triggers.supported)

            limits = {
                "max_model_calls": manifest.resources.limits.max_model_calls,
                "max_tool_calls": manifest.resources.limits.max_tool_calls,
                "input_max_bytes": manifest.resources.limits.input_max_bytes,
                "output_max_bytes": manifest.resources.limits.output_max_bytes,
                "provider_timeout_ms": manifest.resources.limits.provider_timeout_ms,
                "tool_timeout_ms": manifest.resources.limits.tool_timeout_ms,
            }

            return InspectedPackageMetadata(
                package_id=manifest.package_id,
                package_version=manifest.package_version,
                display_name=manifest.package_name,
                manifest_version=manifest.manifest_version.value,
                signer_fingerprint=staged.verified.signer_fingerprint,
                content_digest=staged.verified.content_digest,
                archive_digest=staged.archive_digest,
                min_nervos_version=min_ver,
                max_nervos_version=max_ver,
                is_compatible=is_compatible,
                entrypoint_module=manifest.runtime.entrypoint.split(":")[0],
                entrypoint_object=manifest.runtime.entrypoint.split(":")[1]
                if ":" in manifest.runtime.entrypoint
                else "",
                tools_required=tools_required,
                tools_optional=tools_optional,
                memory_declarations=tuple(memory_decls),
                trigger_declarations=trigger_decls,
                config_schema=schema_json,
                resource_limits=limits,
            )
        finally:
            self._store.cleanup_staging(staged)

    def list_packages(
        self, status: PackageInstallStatus | None = None
    ) -> tuple[PackageVersionSummary, ...]:
        """List installed package releases, sorted by package_id and SemVer descending."""
        summaries = list(self._persistence.list_package_summaries(status=status))

        def sort_key(item: PackageVersionSummary) -> tuple[str, PackageVersion]:
            try:
                version = PackageVersion(item.package_version)
            except ValueError:
                version = PackageVersion("0.0.0")
            return (item.package_id, version)

        summaries.sort(key=sort_key, reverse=True)
        return tuple(summaries)

    def get_package_detail(self, package_id: str, package_version: str) -> PackageVersionDetail:
        return self._persistence.get_package_detail(package_id, package_version)

    def get_removal_plan(self, package_id: str, package_version: str) -> PackageRemovalPlan:
        return self._persistence.get_removal_plan(package_id, package_version)
