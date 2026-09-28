"""SQL package registry persistence and active installed-package definition source."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from time import sleep

from sqlalchemy import Connection, Engine, insert, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import OperationalError

from nervos_core.application.agent_definitions import AgentDefinitionSource
from nervos_core.application.package_installation import PackageInstallRecordInput
from nervos_core.application.package_manifest import (
    parse_package_manifest,
    project_agent_definition,
)
from nervos_core.domain.agents import AgentDefinition, AgentDefinitionId
from nervos_core.domain.execution import RunExecutableSnapshot, RunExecutionKind
from nervos_core.domain.package_installation import (
    AgentInstancePackageBinding,
    InstalledPackageVersion,
    PackageEnvironment,
    PackageEnvironmentStatus,
    PackageInstallStatus,
)
from nervos_core.infrastructure.database.models import (
    AgentInstancePackageBindingRecord,
    AgentInstanceRecord,
    InstalledPackageDependencyRecord,
    InstalledPackageFileRecord,
    InstalledPackageVersionRecord,
    PackageEnvironmentRecord,
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
