"""SQL package registry persistence and active installed-package definition source."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from time import sleep
from typing import cast

from sqlalchemy import Connection, Engine, func, insert, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import OperationalError

from nervos_core.application.agent_definitions import AgentDefinitionSource
from nervos_core.application.agents import AgentInstanceNotFound
from nervos_core.application.package_installation import PackageInstallRecordInput
from nervos_core.application.package_manifest import (
    parse_package_manifest,
    project_agent_definition,
)
from nervos_core.domain.agents import AgentDefinition, AgentDefinitionId, AgentInstance
from nervos_core.domain.execution import RunExecutableSnapshot, RunExecutionKind
from nervos_core.domain.package_installation import (
    AgentInstancePackageBinding,
    InstalledPackageVersion,
    PackageEnvironment,
    PackageEnvironmentStatus,
    PackageInstallStatus,
)
from nervos_core.domain.package_query import (
    PackageHasActiveRuns,
    PackageHasBoundInstances,
    PackageRemovalPlan,
    PackageVersionDetail,
    PackageVersionSummary,
    StaleConfigRevision,
)
from nervos_core.domain.packages import (
    NERVOS_CORE_COMPATIBILITY_VERSION,
    PackageVersion,
)
from nervos_core.domain.runs import RunStatus
from nervos_core.infrastructure.database.models import (
    AgentInstancePackageBindingRecord,
    AgentInstanceRecord,
    InstalledPackageDependencyRecord,
    InstalledPackageFileRecord,
    InstalledPackageVersionRecord,
    PackageEnvironmentRecord,
    RunRecord,
)
from nervos_core.infrastructure.database.transaction import TransactionRunner


class PackageIdentityConflict(ValueError):
    """The exact package identity is already bound to different verified evidence."""


class PackageInstallInProgress(ValueError):
    """An identical package installation is already in progress."""


class PackageRegistryInvariantError(RuntimeError):
    """Durable package registry state violates the G3 contract."""


class InstalledPackageNotFound(LookupError):
    """No exact installed package version exists."""


class SqlAlchemyPackageRegistryPersistence:
    """Short serialized mutations for node-global package state."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._runner = TransactionRunner(engine, sleep)

    def begin_install(self, command: PackageInstallRecordInput) -> InstalledPackageVersion:
        verified = command.verified
        package_id = verified.manifest.package_id
        package_version = verified.manifest.package_version

        def operation(connection: Connection):
            existing = (
                connection.execute(
                    select(InstalledPackageVersionRecord).where(
                        InstalledPackageVersionRecord.package_id == package_id,
                        InstalledPackageVersionRecord.package_version == package_version,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if (
                    existing["content_digest"] != verified.content_digest
                    or existing["signer_fingerprint"] != verified.signer_fingerprint
                    or existing["archive_digest"] != verified.archive_digest
                ):
                    raise PackageIdentityConflict
                status = PackageInstallStatus(str(existing["status"]))
                if status is PackageInstallStatus.INSTALLING:
                    raise PackageInstallInProgress
                if status in {PackageInstallStatus.INSTALLED, PackageInstallStatus.ACTIVE}:
                    return _installed_from_mapping(existing)
                if status is not PackageInstallStatus.FAILED:
                    raise PackageIdentityConflict
                obligations = connection.execute(
                    select(AgentInstancePackageBindingRecord.agent_instance_id).where(
                        AgentInstancePackageBindingRecord.installed_package_version_id
                        == existing["id"]
                    )
                ).first()
                if obligations is not None:
                    raise PackageIdentityConflict
                connection.execute(
                    update(InstalledPackageVersionRecord)
                    .where(InstalledPackageVersionRecord.id == existing["id"])
                    .values(
                        status=PackageInstallStatus.INSTALLING.value,
                        staging_key=command.staging_key,
                        approved_by_user_id=command.authorization.approved_by_user_id,
                        approved_at=command.authorization.approved_at,
                        updated_at=command.now,
                        failed_at=None,
                        last_error_code=None,
                        last_error_message=None,
                    )
                )
                return self._load_on_connection(connection, int(existing["id"]))

            module, symbol = verified.manifest.runtime.entrypoint.split(":", 1)
            result = connection.execute(
                insert(InstalledPackageVersionRecord).values(
                    package_id=package_id,
                    package_version=package_version,
                    status=PackageInstallStatus.INSTALLING.value,
                    content_digest=verified.content_digest,
                    archive_digest=verified.archive_digest,
                    signer_public_key=verified.signer_public_key,
                    signer_fingerprint=verified.signer_fingerprint,
                    manifest_bytes=command.manifest_bytes,
                    config_schema_bytes=command.config_schema_bytes,
                    dependency_lock_bytes=command.dependency_lock_bytes,
                    agent_wheel_name=verified.agent_wheel.name,
                    agent_wheel_version=verified.agent_wheel.version,
                    agent_wheel_sha256=verified.agent_wheel.sha256,
                    agent_wheel_size=verified.agent_wheel.size,
                    entrypoint_module=module,
                    entrypoint_object=symbol,
                    staging_key=command.staging_key,
                    approved_by_user_id=command.authorization.approved_by_user_id,
                    approved_at=command.authorization.approved_at,
                    created_at=command.now,
                    updated_at=command.now,
                )
            )
            pk = result.inserted_primary_key
            if pk is None:
                raise RuntimeError("Insert failed: no primary key returned for installed package")
            installed_id = int(pk[0])
            connection.execute(
                insert(InstalledPackageFileRecord),
                [
                    {
                        "installed_package_version_id": installed_id,
                        "path": entry.path,
                        "sha256": entry.sha256,
                        "byte_length": entry.size,
                    }
                    for entry in verified.entries
                ],
            )
            if verified.dependencies:
                connection.execute(
                    insert(InstalledPackageDependencyRecord),
                    [
                        {
                            "installed_package_version_id": installed_id,
                            "distribution_name": wheel.name,
                            "distribution_version": wheel.version,
                            "filename": wheel.filename,
                            "sha256": wheel.sha256,
                            "byte_length": wheel.size,
                        }
                        for wheel in verified.dependencies
                    ],
                )
            return self._load_on_connection(connection, installed_id)

        return self._runner.run(operation)

    def mark_installed(self, installed_id: int, storage_key: str, now: datetime) -> None:
        def operation(connection: Connection):
            changed = connection.execute(
                update(InstalledPackageVersionRecord)
                .where(
                    InstalledPackageVersionRecord.id == installed_id,
                    InstalledPackageVersionRecord.status == PackageInstallStatus.INSTALLING.value,
                )
                .values(
                    status=PackageInstallStatus.INSTALLED.value,
                    storage_key=storage_key,
                    staging_key=None,
                    installed_at=now,
                    updated_at=now,
                )
            ).rowcount
            if changed != 1:
                raise PackageRegistryInvariantError("package is not installing")

        self._runner.run(operation)

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
    ) -> PackageEnvironment:
        def operation(connection: Connection):
            existing = (
                connection.execute(
                    select(PackageEnvironmentRecord).where(
                        PackageEnvironmentRecord.environment_digest == environment_digest
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is None:
                result = connection.execute(
                    insert(PackageEnvironmentRecord).values(
                        environment_digest=environment_digest,
                        environment_key_json=environment_key_json,
                        environment_key=environment_key,
                        status=PackageEnvironmentStatus.PREPARING.value,
                        python_version="3.12",
                        sdk_version=sdk_version,
                        sdk_wheel_digest=sdk_wheel_digest,
                        host_version=host_version,
                        host_wheel_digest=host_wheel_digest,
                        host_protocol_version="1",
                        created_at=now,
                        updated_at=now,
                    )
                )
                pk = result.inserted_primary_key
                if pk is None:
                    raise RuntimeError("Insert failed: no primary key returned for environment")
                environment_id = int(pk[0])
                return self._load_environment(connection, environment_id)
            if existing["environment_key_json"] != environment_key_json:
                raise PackageIdentityConflict
            return _environment_from_mapping(existing)

        return self._runner.run(operation)

    def mark_environment_ready(self, environment_id: int, now: datetime) -> None:
        def operation(connection: Connection):
            changed = connection.execute(
                update(PackageEnvironmentRecord)
                .where(
                    PackageEnvironmentRecord.id == environment_id,
                    PackageEnvironmentRecord.status.in_(
                        [
                            PackageEnvironmentStatus.PREPARING.value,
                            PackageEnvironmentStatus.READY.value,
                        ]
                    ),
                )
                .values(
                    status=PackageEnvironmentStatus.READY.value,
                    ready_at=now,
                    updated_at=now,
                    failed_at=None,
                    last_error_code=None,
                    last_error_message=None,
                )
            ).rowcount
            if changed != 1:
                raise PackageRegistryInvariantError("environment cannot become ready")

        self._runner.run(operation)

    def attach_environment(self, installed_id: int, environment_id: int, now: datetime) -> None:
        def operation(connection: Connection):
            changed = connection.execute(
                update(InstalledPackageVersionRecord)
                .where(
                    InstalledPackageVersionRecord.id == installed_id,
                    InstalledPackageVersionRecord.status == PackageInstallStatus.INSTALLED.value,
                )
                .values(environment_id=environment_id, updated_at=now)
            ).rowcount
            if changed != 1:
                raise PackageRegistryInvariantError("installed package cannot attach environment")

        self._runner.run(operation)

    def mark_active(self, installed_id: int, now: datetime) -> InstalledPackageVersion:
        def operation(connection: Connection):
            environment_ready = connection.execute(
                select(PackageEnvironmentRecord.id)
                .join(
                    InstalledPackageVersionRecord,
                    InstalledPackageVersionRecord.environment_id == PackageEnvironmentRecord.id,
                )
                .where(
                    InstalledPackageVersionRecord.id == installed_id,
                    InstalledPackageVersionRecord.status == PackageInstallStatus.INSTALLED.value,
                    PackageEnvironmentRecord.status == PackageEnvironmentStatus.READY.value,
                )
            ).scalar_one_or_none()
            if environment_ready is None:
                raise PackageRegistryInvariantError("package environment is not ready")
            connection.execute(
                update(InstalledPackageVersionRecord)
                .where(InstalledPackageVersionRecord.id == installed_id)
                .values(
                    status=PackageInstallStatus.ACTIVE.value,
                    activated_at=now,
                    updated_at=now,
                )
            )
            return self._load_on_connection(connection, installed_id)

        return self._runner.run(operation)

    def mark_failed(self, installed_id: int, code: str, message: str, now: datetime) -> None:
        def operation(connection: Connection):
            connection.execute(
                update(InstalledPackageVersionRecord)
                .where(
                    InstalledPackageVersionRecord.id == installed_id,
                    InstalledPackageVersionRecord.status.in_(
                        [
                            PackageInstallStatus.INSTALLING.value,
                            PackageInstallStatus.INSTALLED.value,
                        ]
                    ),
                )
                .values(
                    status=PackageInstallStatus.FAILED.value,
                    failed_at=now,
                    updated_at=now,
                    last_error_code=code,
                    last_error_message=message,
                )
            )

        self._runner.run(operation)

    def load(self, installed_id: int) -> InstalledPackageVersion:
        with self._engine.connect() as connection:
            return self._load_on_connection(connection, installed_id)

    def load_verified_bytes(self, installed_id: int) -> tuple[bytes, bytes, bytes]:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(
                    InstalledPackageVersionRecord.manifest_bytes,
                    InstalledPackageVersionRecord.config_schema_bytes,
                    InstalledPackageVersionRecord.dependency_lock_bytes,
                ).where(InstalledPackageVersionRecord.id == installed_id)
            ).one_or_none()
            if row is None:
                raise InstalledPackageNotFound
            return bytes(row[0]), bytes(row[1]), bytes(row[2])

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
    ) -> AgentInstancePackageBinding:
        def operation(connection: Connection):
            package = (
                connection.execute(
                    select(InstalledPackageVersionRecord).where(
                        InstalledPackageVersionRecord.id == installed_id,
                        InstalledPackageVersionRecord.status == PackageInstallStatus.ACTIVE.value,
                    )
                )
                .mappings()
                .one_or_none()
            )
            instance = (
                connection.execute(
                    select(AgentInstanceRecord).where(
                        AgentInstanceRecord.id == agent_instance_id,
                        AgentInstanceRecord.owner_user_id == owner_user_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if package is None or instance is None:
                raise InstalledPackageNotFound
            if (
                instance["agent_key"] != package["package_id"]
                or instance["agent_definition_version"] != package["package_version"]
            ):
                raise PackageRegistryInvariantError(
                    "AgentInstance definition does not match package"
                )
            existing = (
                connection.execute(
                    select(AgentInstancePackageBindingRecord.config_revision).where(
                        AgentInstancePackageBindingRecord.agent_instance_id == agent_instance_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is None:
                config_revision = 1
                connection.execute(
                    insert(AgentInstancePackageBindingRecord).values(
                        agent_instance_id=agent_instance_id,
                        installed_package_version_id=installed_id,
                        effective_config_json=effective_config_json,
                        effective_config_digest=effective_config_digest,
                        config_revision=config_revision,
                        config_schema_digest=config_schema_digest,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                config_revision = int(existing["config_revision"]) + 1
                connection.execute(
                    update(AgentInstancePackageBindingRecord)
                    .where(AgentInstancePackageBindingRecord.agent_instance_id == agent_instance_id)
                    .values(
                        installed_package_version_id=installed_id,
                        effective_config_json=effective_config_json,
                        effective_config_digest=effective_config_digest,
                        config_revision=config_revision,
                        config_schema_digest=config_schema_digest,
                        updated_at=now,
                    )
                )
            return AgentInstancePackageBinding(
                agent_instance_id,
                installed_id,
                effective_config_json,
                effective_config_digest,
                config_revision,
                config_schema_digest,
            )

        return self._runner.run(operation)

    def list_package_summaries(
        self, status: PackageInstallStatus | None = None
    ) -> tuple[PackageVersionSummary, ...]:
        def operation(connection: Connection) -> tuple[PackageVersionSummary, ...]:
            counts_subquery = (
                select(
                    AgentInstancePackageBindingRecord.installed_package_version_id.label(
                        "installed_id"
                    ),
                    func.count(AgentInstancePackageBindingRecord.agent_instance_id).label(
                        "bound_count"
                    ),
                )
                .group_by(AgentInstancePackageBindingRecord.installed_package_version_id)
                .subquery()
            )
            query = select(
                InstalledPackageVersionRecord.package_id,
                InstalledPackageVersionRecord.package_version,
                InstalledPackageVersionRecord.manifest_bytes,
                InstalledPackageVersionRecord.status,
                InstalledPackageVersionRecord.signer_fingerprint,
                InstalledPackageVersionRecord.content_digest,
                InstalledPackageVersionRecord.archive_digest,
                InstalledPackageVersionRecord.installed_at,
                InstalledPackageVersionRecord.activated_at,
                InstalledPackageVersionRecord.failed_at,
                InstalledPackageVersionRecord.removed_at,
                InstalledPackageVersionRecord.last_error_code,
                InstalledPackageVersionRecord.last_error_message,
                func.coalesce(counts_subquery.c.bound_count, 0).label("bound_count"),
            ).outerjoin(
                counts_subquery,
                InstalledPackageVersionRecord.id == counts_subquery.c.installed_id,
            )
            if status is not None:
                query = query.where(InstalledPackageVersionRecord.status == status.value)
            rows = (
                connection.execute(
                    query.order_by(
                        InstalledPackageVersionRecord.package_id,
                        InstalledPackageVersionRecord.package_version.desc(),
                    )
                )
                .mappings()
                .all()
            )

            summaries: list[PackageVersionSummary] = []
            for r in rows:
                manifest = parse_package_manifest(bytes(r["manifest_bytes"]))
                summaries.append(
                    PackageVersionSummary(
                        package_id=str(r["package_id"]),
                        package_version=str(r["package_version"]),
                        display_name=manifest.display_name,
                        status=PackageInstallStatus(str(r["status"])),
                        signer_fingerprint=str(r["signer_fingerprint"]),
                        content_digest=str(r["content_digest"]),
                        archive_digest=str(r["archive_digest"]),
                        bound_instances_count=int(r["bound_count"]),
                        installed_at=r["installed_at"],
                        activated_at=r["activated_at"],
                        failed_at=r["failed_at"],
                        removed_at=r["removed_at"],
                        last_error_code=r["last_error_code"],
                        last_error_message=r["last_error_message"],
                    )
                )
            return tuple(summaries)

        return self._runner.run(operation)

    def get_package_detail(self, package_id: str, package_version: str) -> PackageVersionDetail:
        def operation(connection: Connection) -> PackageVersionDetail:
            return _load_package_detail_on_connection(connection, package_id, package_version)

        return self._runner.run(operation)

    def get_package_detail_for_instance(
        self, agent_instance_id: int, owner_user_id: int
    ) -> PackageVersionDetail:
        def operation(connection: Connection) -> PackageVersionDetail:
            instance = (
                connection.execute(
                    select(AgentInstanceRecord).where(
                        AgentInstanceRecord.id == agent_instance_id,
                        AgentInstanceRecord.owner_user_id == owner_user_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if instance is None:
                raise AgentInstanceNotFound
            return _load_package_detail_on_connection(
                connection,
                str(instance["agent_key"]),
                str(instance["agent_definition_version"]),
            )

        return self._runner.run(operation)

    def get_removal_plan(self, package_id: str, package_version: str) -> PackageRemovalPlan:
        def operation(connection: Connection) -> PackageRemovalPlan:
            row = (
                connection.execute(
                    select(InstalledPackageVersionRecord).where(
                        InstalledPackageVersionRecord.package_id == package_id,
                        InstalledPackageVersionRecord.package_version == package_version,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise InstalledPackageNotFound

            status = PackageInstallStatus(str(row["status"]))
            bound_instances = [
                int(r[0])
                for r in connection.execute(
                    select(AgentInstancePackageBindingRecord.agent_instance_id).where(
                        AgentInstancePackageBindingRecord.installed_package_version_id == row["id"]
                    )
                ).all()
            ]

            nonterminal_runs = [
                int(r[0])
                for r in connection.execute(
                    select(RunRecord.id).where(
                        RunRecord.installed_package_version_id == row["id"],
                        RunRecord.status.in_([RunStatus.CREATED.value, RunStatus.RUNNING.value]),
                    )
                ).all()
            ]

            env_id = row["environment_id"]
            is_env_shared = False
            if env_id is not None:
                other_env_user = connection.execute(
                    select(InstalledPackageVersionRecord.id).where(
                        InstalledPackageVersionRecord.environment_id == env_id,
                        InstalledPackageVersionRecord.id != row["id"],
                        InstalledPackageVersionRecord.status != PackageInstallStatus.REMOVED.value,
                    )
                ).first()
                run_ref = connection.execute(
                    select(RunRecord.id).where(RunRecord.package_environment_id == env_id)
                ).first()
                if other_env_user is not None or run_ref is not None:
                    is_env_shared = True

            blocking_reasons: list[str] = []
            if bound_instances:
                blocking_reasons.append(
                    f"{len(bound_instances)} agent instance(s) are bound to this package version."
                )
            if nonterminal_runs:
                blocking_reasons.append(
                    f"{len(nonterminal_runs)} run(s) are active under this package version."
                )
            if status is PackageInstallStatus.REMOVED:
                blocking_reasons.append("The package version is already removed.")

            can_remove_immediately = (
                len(bound_instances) == 0
                and len(nonterminal_runs) == 0
                and status is not PackageInstallStatus.REMOVED
            )
            can_begin_removal = (
                len(bound_instances) == 0 and status is not PackageInstallStatus.REMOVED
            )

            return PackageRemovalPlan(
                package_id=package_id,
                package_version=package_version,
                status=status,
                bound_instance_ids=tuple(bound_instances),
                bound_instances_count=len(bound_instances),
                nonterminal_run_ids=tuple(nonterminal_runs),
                nonterminal_runs_count=len(nonterminal_runs),
                is_environment_shared=is_env_shared,
                can_remove_immediately=can_remove_immediately,
                can_begin_removal=can_begin_removal,
                blocking_reasons=tuple(blocking_reasons),
            )

        return self._runner.run(operation)

    def get_installed_id(self, package_id: str, package_version: str) -> int:
        def operation(connection: Connection) -> int:
            row = connection.execute(
                select(InstalledPackageVersionRecord.id).where(
                    InstalledPackageVersionRecord.package_id == package_id,
                    InstalledPackageVersionRecord.package_version == package_version,
                )
            ).first()
            if row is None:
                raise InstalledPackageNotFound
            return int(row[0])

        return self._runner.run(operation)

    def get_binding_for_instance(self, agent_instance_id: int) -> AgentInstancePackageBinding:
        def operation(connection: Connection) -> AgentInstancePackageBinding:
            row = (
                connection.execute(
                    select(AgentInstancePackageBindingRecord).where(
                        AgentInstancePackageBindingRecord.agent_instance_id == agent_instance_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise AgentInstanceNotFound
            return AgentInstancePackageBinding(
                int(row["agent_instance_id"]),
                int(row["installed_package_version_id"]),
                str(row["effective_config_json"]),
                str(row["effective_config_digest"]),
                int(row["config_revision"]),
                str(row["config_schema_digest"]),
            )

        return self._runner.run(operation)

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
    ) -> tuple[AgentInstance, AgentInstancePackageBinding]:
        def operation(connection: Connection):
            pkg = (
                connection.execute(
                    select(InstalledPackageVersionRecord).where(
                        InstalledPackageVersionRecord.package_id == package_id,
                        InstalledPackageVersionRecord.package_version == package_version,
                        InstalledPackageVersionRecord.status == PackageInstallStatus.ACTIVE.value,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if pkg is None:
                raise PackageRegistryInvariantError("package version is not active")

            instance_res = connection.execute(
                insert(AgentInstanceRecord).values(
                    owner_user_id=owner_user_id,
                    agent_key=package_id,
                    agent_definition_version=package_version,
                    display_name=display_name,
                    enabled=True,
                    model_provider=model_provider,
                    model_name=model_name,
                    created_at=now,
                    updated_at=now,
                )
            )
            pk = instance_res.inserted_primary_key
            if pk is None or len(pk) == 0:
                raise RuntimeError("No primary key returned for agent instance")
            instance_id = int(str(pk[0]))

            connection.execute(
                insert(AgentInstancePackageBindingRecord).values(
                    agent_instance_id=instance_id,
                    installed_package_version_id=int(pkg["id"]),
                    effective_config_json=effective_config_json,
                    effective_config_digest=effective_config_digest,
                    config_revision=1,
                    config_schema_digest=config_schema_digest,
                    created_at=now,
                    updated_at=now,
                )
            )

            instance = AgentInstance(
                id=instance_id,
                owner_user_id=owner_user_id,
                definition_id=AgentDefinitionId(package_id, package_version),
                display_name=display_name,
                enabled=True,
                model_provider=model_provider,
                model_name=model_name,
                created_at=now,
                updated_at=now,
            )
            binding = AgentInstancePackageBinding(
                agent_instance_id=instance_id,
                installed_package_version_id=int(pkg["id"]),
                effective_config_json=effective_config_json,
                effective_config_digest=effective_config_digest,
                config_revision=1,
                config_schema_digest=config_schema_digest,
            )
            return instance, binding

        return self._runner.run(operation)

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
    ) -> AgentInstancePackageBinding:
        def operation(connection: Connection):
            instance = (
                connection.execute(
                    select(AgentInstanceRecord).where(
                        AgentInstanceRecord.id == agent_instance_id,
                        AgentInstanceRecord.owner_user_id == owner_user_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if instance is None:
                raise AgentInstanceNotFound

            binding = (
                connection.execute(
                    select(AgentInstancePackageBindingRecord).where(
                        AgentInstancePackageBindingRecord.agent_instance_id == agent_instance_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if binding is None:
                raise AgentInstanceNotFound

            if int(binding["config_revision"]) != expected_config_revision:
                cur = binding["config_revision"]
                raise StaleConfigRevision(
                    f"expected revision {expected_config_revision}, found {cur}"
                )

            new_revision = expected_config_revision + 1
            connection.execute(
                update(AgentInstancePackageBindingRecord)
                .where(AgentInstancePackageBindingRecord.agent_instance_id == agent_instance_id)
                .values(
                    effective_config_json=effective_config_json,
                    effective_config_digest=effective_config_digest,
                    config_revision=new_revision,
                    config_schema_digest=config_schema_digest,
                    updated_at=now,
                )
            )
            return AgentInstancePackageBinding(
                agent_instance_id=agent_instance_id,
                installed_package_version_id=int(binding["installed_package_version_id"]),
                effective_config_json=effective_config_json,
                effective_config_digest=effective_config_digest,
                config_revision=new_revision,
                config_schema_digest=config_schema_digest,
            )

        return self._runner.run(operation)

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
    ) -> tuple[AgentInstance, AgentInstancePackageBinding]:
        def operation(connection: Connection):
            instance = (
                connection.execute(
                    select(AgentInstanceRecord).where(
                        AgentInstanceRecord.id == agent_instance_id,
                        AgentInstanceRecord.owner_user_id == owner_user_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if instance is None:
                raise AgentInstanceNotFound

            binding = (
                connection.execute(
                    select(AgentInstancePackageBindingRecord).where(
                        AgentInstancePackageBindingRecord.agent_instance_id == agent_instance_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if binding is None:
                raise AgentInstanceNotFound

            if int(binding["config_revision"]) != expected_config_revision:
                cur = binding["config_revision"]
                raise StaleConfigRevision(
                    f"expected revision {expected_config_revision}, found {cur}"
                )

            pkg = (
                connection.execute(
                    select(InstalledPackageVersionRecord).where(
                        InstalledPackageVersionRecord.id == target_installed_id,
                        InstalledPackageVersionRecord.status == PackageInstallStatus.ACTIVE.value,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if pkg is None:
                raise PackageRegistryInvariantError("target package version is not active")

            if (
                pkg["package_id"] != target_package_id
                or pkg["package_version"] != target_package_version
            ):
                raise PackageRegistryInvariantError("target package identity mismatch")

            new_revision = expected_config_revision + 1
            connection.execute(
                update(AgentInstanceRecord)
                .where(AgentInstanceRecord.id == agent_instance_id)
                .values(
                    agent_key=target_package_id,
                    agent_definition_version=target_package_version,
                    updated_at=now,
                )
            )

            connection.execute(
                update(AgentInstancePackageBindingRecord)
                .where(AgentInstancePackageBindingRecord.agent_instance_id == agent_instance_id)
                .values(
                    installed_package_version_id=target_installed_id,
                    effective_config_json=effective_config_json,
                    effective_config_digest=effective_config_digest,
                    config_revision=new_revision,
                    config_schema_digest=config_schema_digest,
                    updated_at=now,
                )
            )

            updated_instance = AgentInstance(
                id=agent_instance_id,
                owner_user_id=owner_user_id,
                definition_id=AgentDefinitionId(target_package_id, target_package_version),
                display_name=str(instance["display_name"]),
                enabled=bool(instance["enabled"]),
                model_provider=str(instance["model_provider"]),
                model_name=str(instance["model_name"]),
                created_at=instance["created_at"],
                updated_at=now,
            )
            updated_binding = AgentInstancePackageBinding(
                agent_instance_id=agent_instance_id,
                installed_package_version_id=target_installed_id,
                effective_config_json=effective_config_json,
                effective_config_digest=effective_config_digest,
                config_revision=new_revision,
                config_schema_digest=config_schema_digest,
            )
            return updated_instance, updated_binding

        return self._runner.run(operation)

    def mark_pending_removal(self, installed_id: int, now: datetime) -> None:
        def operation(connection: Connection):
            connection.execute(
                update(InstalledPackageVersionRecord)
                .where(
                    InstalledPackageVersionRecord.id == installed_id,
                    InstalledPackageVersionRecord.status.in_(
                        [
                            PackageInstallStatus.ACTIVE.value,
                            PackageInstallStatus.INSTALLED.value,
                            PackageInstallStatus.FAILED.value,
                        ]
                    ),
                )
                .values(
                    status=PackageInstallStatus.PENDING_REMOVAL.value,
                    updated_at=now,
                )
            )

        self._runner.run(operation)

    def finalize_removal(
        self, installed_id: int, now: datetime
    ) -> tuple[str | None, str | None, bool]:
        """Finalize removal in DB if 0 obligations remain.

        Returns (storage_key, environment_key, is_env_unshared).
        """

        def operation(connection: Connection) -> tuple[str | None, str | None, bool]:
            row = (
                connection.execute(
                    select(InstalledPackageVersionRecord).where(
                        InstalledPackageVersionRecord.id == installed_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise InstalledPackageNotFound

            bound = connection.scalar(
                select(func.count(AgentInstancePackageBindingRecord.agent_instance_id)).where(
                    AgentInstancePackageBindingRecord.installed_package_version_id == installed_id
                )
            )
            if bound and bound > 0:
                raise PackageHasBoundInstances

            runs = connection.scalar(
                select(func.count(RunRecord.id)).where(
                    RunRecord.installed_package_version_id == installed_id,
                    RunRecord.status.in_([RunStatus.CREATED.value, RunStatus.RUNNING.value]),
                )
            )
            if runs and runs > 0:
                raise PackageHasActiveRuns

            storage_key = str(row["storage_key"]) if row["storage_key"] is not None else None
            env_id = row["environment_id"]
            env_key: str | None = None
            is_env_unshared = False
            if env_id is not None:
                env_row = (
                    connection.execute(
                        select(PackageEnvironmentRecord).where(
                            PackageEnvironmentRecord.id == env_id
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if env_row is not None:
                    env_key = str(env_row["environment_key"])
                    other_users = (
                        connection.scalar(
                            select(func.count(InstalledPackageVersionRecord.id)).where(
                                InstalledPackageVersionRecord.environment_id == env_id,
                                InstalledPackageVersionRecord.id != installed_id,
                                InstalledPackageVersionRecord.status
                                != PackageInstallStatus.REMOVED.value,
                            )
                        )
                        or 0
                    )
                    run_references = (
                        connection.scalar(
                            select(func.count(RunRecord.id)).where(
                                RunRecord.package_environment_id == env_id
                            )
                        )
                        or 0
                    )
                    is_env_unshared = other_users == 0 and run_references == 0

            connection.execute(
                update(InstalledPackageVersionRecord)
                .where(InstalledPackageVersionRecord.id == installed_id)
                .values(
                    status=PackageInstallStatus.REMOVED.value,
                    removed_at=now,
                    updated_at=now,
                )
            )
            return storage_key, env_key, is_env_unshared

        return self._runner.run(operation)

    def reconcile_pending_removals(
        self, now: datetime
    ) -> list[tuple[int, str, str, str | None, str | None, bool]]:
        """Find pending removals that can be safely finalized.

        Returns list of (id, package_id, version, storage_key, env_key, is_env_unshared).
        """

        def operation(
            connection: Connection,
        ) -> list[tuple[int, str, str, str | None, str | None, bool]]:
            pending_rows = (
                connection.execute(
                    select(InstalledPackageVersionRecord).where(
                        InstalledPackageVersionRecord.status
                        == PackageInstallStatus.PENDING_REMOVAL.value
                    )
                )
                .mappings()
                .all()
            )
            ready: list[tuple[int, str, str, str | None, str | None, bool]] = []
            for row in pending_rows:
                inst_id = int(row["id"])
                bound = connection.scalar(
                    select(func.count(AgentInstancePackageBindingRecord.agent_instance_id)).where(
                        AgentInstancePackageBindingRecord.installed_package_version_id == inst_id
                    )
                )
                if bound and bound > 0:
                    continue
                runs = connection.scalar(
                    select(func.count(RunRecord.id)).where(
                        RunRecord.installed_package_version_id == inst_id,
                        RunRecord.status.in_([RunStatus.CREATED.value, RunStatus.RUNNING.value]),
                    )
                )
                if runs and runs > 0:
                    continue

                storage_key = str(row["storage_key"]) if row["storage_key"] is not None else None
                env_id = row["environment_id"]
                env_key: str | None = None
                is_env_unshared = False
                if env_id is not None:
                    env_row = (
                        connection.execute(
                            select(PackageEnvironmentRecord).where(
                                PackageEnvironmentRecord.id == env_id
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if env_row is not None:
                        env_key = str(env_row["environment_key"])
                        other_users = (
                            connection.scalar(
                                select(func.count(InstalledPackageVersionRecord.id)).where(
                                    InstalledPackageVersionRecord.environment_id == env_id,
                                    InstalledPackageVersionRecord.id != inst_id,
                                    InstalledPackageVersionRecord.status
                                    != PackageInstallStatus.REMOVED.value,
                                )
                            )
                            or 0
                        )
                        run_references = (
                            connection.scalar(
                                select(func.count(RunRecord.id)).where(
                                    RunRecord.package_environment_id == env_id
                                )
                            )
                            or 0
                        )
                        is_env_unshared = other_users == 0 and run_references == 0

                connection.execute(
                    update(InstalledPackageVersionRecord)
                    .where(InstalledPackageVersionRecord.id == inst_id)
                    .values(
                        status=PackageInstallStatus.REMOVED.value,
                        removed_at=now,
                        updated_at=now,
                    )
                )
                ready.append(
                    (
                        inst_id,
                        str(row["package_id"]),
                        str(row["package_version"]),
                        storage_key,
                        env_key,
                        is_env_unshared,
                    )
                )
            return ready

        return self._runner.run(operation)

    def _load_on_connection(
        self, connection: Connection, installed_id: int
    ) -> InstalledPackageVersion:
        row = (
            connection.execute(
                select(InstalledPackageVersionRecord).where(
                    InstalledPackageVersionRecord.id == installed_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise InstalledPackageNotFound
        return _installed_from_mapping(row)

    @staticmethod
    def _load_environment(connection: Connection, environment_id: int) -> PackageEnvironment:
        row = (
            connection.execute(
                select(PackageEnvironmentRecord).where(
                    PackageEnvironmentRecord.id == environment_id
                )
            )
            .mappings()
            .one()
        )
        return _environment_from_mapping(row)


class SqlInstalledPackageDefinitionSource(AgentDefinitionSource):
    """Restart-safe exact definitions projected from active verified manifest bytes.

    Composition is allowed before migration 0013 is applied. In that pre-G3 schema, absence of the
    package table means no installed definitions; it must not break unrelated API startup. Other
    SQL errors still propagate fail-closed.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def list_definitions(self) -> tuple[AgentDefinition, ...]:
        database = self._engine.url.database
        if database is not None and not Path(database).exists():
            return ()
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(
                    select(InstalledPackageVersionRecord.manifest_bytes)
                    .where(
                        InstalledPackageVersionRecord.status == PackageInstallStatus.ACTIVE.value
                    )
                    .order_by(
                        InstalledPackageVersionRecord.package_id,
                        InstalledPackageVersionRecord.package_version,
                    )
                ).all()
        except OperationalError as error:
            if "no such table: installed_package_versions" in str(error).lower():
                return ()
            raise
        return tuple(
            project_agent_definition(parse_package_manifest(bytes(row[0]))) for row in rows
        )


def resolve_run_execution_snapshot_on_connection(
    connection: Connection, *, agent_instance_id: int, definition_id: AgentDefinitionId
) -> RunExecutableSnapshot:
    row = (
        connection.execute(
            select(
                InstalledPackageVersionRecord.id.label("installed_id"),
                InstalledPackageVersionRecord.package_id,
                InstalledPackageVersionRecord.package_version,
                InstalledPackageVersionRecord.status.label("package_status"),
                InstalledPackageVersionRecord.content_digest,
                InstalledPackageVersionRecord.entrypoint_module,
                InstalledPackageVersionRecord.entrypoint_object,
                InstalledPackageVersionRecord.environment_id,
                AgentInstancePackageBindingRecord.effective_config_json,
                AgentInstancePackageBindingRecord.effective_config_digest,
                AgentInstancePackageBindingRecord.config_revision,
                PackageEnvironmentRecord.environment_digest,
                PackageEnvironmentRecord.status.label("environment_status"),
                PackageEnvironmentRecord.host_protocol_version,
                PackageEnvironmentRecord.sdk_version,
            )
            .join(
                InstalledPackageVersionRecord,
                AgentInstancePackageBindingRecord.installed_package_version_id
                == InstalledPackageVersionRecord.id,
            )
            .join(
                PackageEnvironmentRecord,
                InstalledPackageVersionRecord.environment_id == PackageEnvironmentRecord.id,
            )
            .where(AgentInstancePackageBindingRecord.agent_instance_id == agent_instance_id)
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return RunExecutableSnapshot(
            execution_kind=RunExecutionKind.BUILTIN,
            installed_package_version_id=None,
            package_content_digest=None,
            package_environment_id=None,
            package_environment_digest=None,
            package_entrypoint=None,
            effective_config_json="{}",
            effective_config_digest=None,
            agent_instance_config_revision=None,
            host_protocol_version=None,
            sdk_api_version=None,
        )
    if (
        row["package_id"] != definition_id.agent_key
        or row["package_version"] != definition_id.agent_definition_version
        or row["package_status"] != PackageInstallStatus.ACTIVE.value
        or row["environment_status"] != PackageEnvironmentStatus.READY.value
    ):
        raise PackageRegistryInvariantError("package binding is not executable")
    entrypoint_module = str(row["entrypoint_module"])
    entrypoint_object = str(row["entrypoint_object"])
    return RunExecutableSnapshot(
        execution_kind=RunExecutionKind.PACKAGE,
        installed_package_version_id=int(row["installed_id"]),
        package_content_digest=str(row["content_digest"]),
        package_environment_id=int(row["environment_id"]),
        package_environment_digest=str(row["environment_digest"]),
        package_entrypoint=f"{entrypoint_module}:{entrypoint_object}",
        effective_config_json=str(row["effective_config_json"]),
        effective_config_digest=str(row["effective_config_digest"]),
        agent_instance_config_revision=int(row["config_revision"]),
        host_protocol_version=str(row["host_protocol_version"]),
        sdk_api_version=str(row["sdk_version"]),
    )


def _load_package_detail_on_connection(
    connection: Connection, package_id: str, package_version: str
) -> PackageVersionDetail:
    row = (
        connection.execute(
            select(
                InstalledPackageVersionRecord,
                PackageEnvironmentRecord.status.label("env_status"),
            )
            .outerjoin(
                PackageEnvironmentRecord,
                InstalledPackageVersionRecord.environment_id == PackageEnvironmentRecord.id,
            )
            .where(
                InstalledPackageVersionRecord.package_id == package_id,
                InstalledPackageVersionRecord.package_version == package_version,
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise InstalledPackageNotFound

    bound_count = (
        connection.scalar(
            select(func.count(AgentInstancePackageBindingRecord.agent_instance_id)).where(
                AgentInstancePackageBindingRecord.installed_package_version_id == row["id"]
            )
        )
        or 0
    )

    manifest = parse_package_manifest(bytes(row["manifest_bytes"]))
    parsed_schema: object = json.loads(bytes(row["config_schema_bytes"]).decode("utf-8"))
    schema_json: dict[str, object] = (
        {str(k): v for k, v in cast("dict[object, object]", parsed_schema).items()}
        if isinstance(parsed_schema, dict)
        else {}
    )

    curr = PackageVersion(NERVOS_CORE_COMPATIBILITY_VERSION)
    is_compatible = manifest.nervos.min_version <= curr <= manifest.nervos.max_version
    memory_decls: list[str] = []
    if manifest.memory.reads:
        memory_decls.append("reads")
    if manifest.memory.writes:
        memory_decls.append("writes")

    limits = {
        "max_model_calls": manifest.resources.limits.max_model_calls,
        "max_tool_calls": manifest.resources.limits.max_tool_calls,
        "input_max_bytes": manifest.resources.limits.input_max_bytes,
        "output_max_bytes": manifest.resources.limits.output_max_bytes,
        "provider_timeout_ms": manifest.resources.limits.provider_timeout_ms,
        "tool_timeout_ms": manifest.resources.limits.tool_timeout_ms,
    }

    env_status = (
        PackageEnvironmentStatus(str(row["env_status"])) if row["env_status"] is not None else None
    )

    return PackageVersionDetail(
        package_id=str(row["package_id"]),
        package_version=str(row["package_version"]),
        display_name=manifest.display_name,
        status=PackageInstallStatus(str(row["status"])),
        signer_fingerprint=str(row["signer_fingerprint"]),
        content_digest=str(row["content_digest"]),
        archive_digest=str(row["archive_digest"]),
        manifest_version=manifest.manifest_version.value,
        min_nervos_version=manifest.nervos.min_version.value,
        max_nervos_version=manifest.nervos.max_version.value,
        is_compatible=is_compatible,
        entrypoint_module=str(row["entrypoint_module"]),
        entrypoint_object=str(row["entrypoint_object"]),
        tools_required=tuple(manifest.tools.required),
        tools_optional=tuple(manifest.tools.optional),
        memory_declarations=tuple(memory_decls),
        trigger_declarations=tuple(k.value for k in manifest.triggers.supported),
        config_schema=schema_json,
        resource_limits=limits,
        bound_instances_count=int(bound_count),
        environment_id=int(row["environment_id"]) if row["environment_id"] is not None else None,
        environment_status=env_status,
        installed_at=row["installed_at"],
        activated_at=row["activated_at"],
        failed_at=row["failed_at"],
        removed_at=row["removed_at"],
        last_error_code=row["last_error_code"],
        last_error_message=row["last_error_message"],
    )


def _installed_from_mapping(row: RowMapping) -> InstalledPackageVersion:
    return InstalledPackageVersion(
        id=int(row["id"]),
        package_id=str(row["package_id"]),
        package_version=str(row["package_version"]),
        status=PackageInstallStatus(str(row["status"])),
        content_digest=str(row["content_digest"]),
        archive_digest=str(row["archive_digest"]),
        signer_fingerprint=str(row["signer_fingerprint"]),
        storage_key=str(row["storage_key"]) if row["storage_key"] is not None else None,
        environment_id=int(row["environment_id"]) if row["environment_id"] is not None else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _environment_from_mapping(row: RowMapping) -> PackageEnvironment:
    return PackageEnvironment(
        id=int(row["id"]),
        environment_digest=str(row["environment_digest"]),
        environment_key=str(row["environment_key"]),
        status=PackageEnvironmentStatus(str(row["status"])),
        python_version=str(row["python_version"]),
        sdk_version=str(row["sdk_version"]),
        host_version=str(row["host_version"]),
        host_protocol_version=str(row["host_protocol_version"]),
    )
