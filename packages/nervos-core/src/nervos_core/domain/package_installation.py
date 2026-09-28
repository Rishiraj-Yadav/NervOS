"""Domain values for installed package versions, environments, and execution pins."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from nervos_core.domain.packages import PackageVersion, validate_package_id

_ERROR_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")

_HEX64 = frozenset("0123456789abcdef")

_HOST_PROTOCOL_VERSION = "1"


class InvalidPackageInstallation(ValueError):
    """Raised when a durable package lifecycle value is malformed."""


class PackageInstallStatus(StrEnum):
    INSTALLING = "installing"
    INSTALLED = "installed"
    ACTIVE = "active"
    FAILED = "failed"
    PENDING_REMOVAL = "pending_removal"
    REMOVED = "removed"


class PackageEnvironmentStatus(StrEnum):
    PREPARING = "preparing"
    READY = "ready"
    FAILED = "failed"


def validate_sha256(value: str, *, field: str = "digest") -> str:
    if len(value) != 64 or any(character not in _HEX64 for character in value):
        raise InvalidPackageInstallation(f"invalid {field}")
    return value


def validate_storage_key(value: str, *, field: str = "storage key") -> str:
    if not value or value.startswith(("/", "\\")) or "\x00" in value or ".." in value.split("/"):
        raise InvalidPackageInstallation(f"invalid {field}")
    if "\\" in value or "//" in value or value.endswith("/") or len(value.encode("utf-8")) > 512:
        raise InvalidPackageInstallation(f"invalid {field}")
    return value


def validate_package_status(value: str) -> PackageInstallStatus:
    try:
        return PackageInstallStatus(value)
    except ValueError as error:
        raise InvalidPackageInstallation("invalid package lifecycle status") from error


def validate_environment_status(value: str) -> PackageEnvironmentStatus:
    try:
        return PackageEnvironmentStatus(value)
    except ValueError as error:
        raise InvalidPackageInstallation("invalid package environment status") from error


def validate_safe_package_error(
    code: str | None, message: str | None
) -> tuple[str | None, str | None]:
    if (code is None) != (message is None):
        raise InvalidPackageInstallation("package error code/message must appear together")
    if code is not None:
        if _ERROR_CODE_PATTERN.fullmatch(code) is None:
            raise InvalidPackageInstallation("invalid package error code")
        if not message or len(message) > 512 or "\x00" in message:
            raise InvalidPackageInstallation("invalid package error message")
    return code, message


@dataclass(frozen=True, slots=True)
class PackageInstallAuthorization:
    """Explicit per-install operator trust, bound to verified artifact identity (ADR 0026)."""

    package_id: str
    package_version: str
    content_digest: str
    signer_fingerprint: str
    approved_by_user_id: int
    approved_at: datetime
    archive_digest: str | None = None

    def __post_init__(self) -> None:
        validate_package_id(self.package_id)
        PackageVersion(self.package_version)
        validate_sha256(self.content_digest, field="content digest")
        validate_sha256(self.signer_fingerprint, field="signer fingerprint")
        if self.archive_digest is not None:
            validate_sha256(self.archive_digest, field="archive digest")
        if self.approved_by_user_id <= 0:
            raise InvalidPackageInstallation("approved_by_user_id must be positive")


@dataclass(frozen=True, slots=True)
class InstalledPackageVersion:
    """One node-global exact installed package release (ADR 0025)."""

    id: int
    package_id: str
    package_version: str
    status: PackageInstallStatus
    content_digest: str
    archive_digest: str
    signer_fingerprint: str
    storage_key: str | None
    environment_id: int | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.id <= 0:
            raise InvalidPackageInstallation("installed package id must be positive")
        validate_package_id(self.package_id)
        PackageVersion(self.package_version)
        validate_sha256(self.content_digest, field="content digest")
        validate_sha256(self.archive_digest, field="archive digest")
        validate_sha256(self.signer_fingerprint, field="signer fingerprint")
        if self.storage_key is not None:
            validate_storage_key(self.storage_key)
        if self.environment_id is not None and self.environment_id <= 0:
            raise InvalidPackageInstallation("environment_id must be positive")


@dataclass(frozen=True, slots=True)
class PackageEnvironment:
    """One content-addressed isolated Python environment (ADR 0026)."""

    id: int
    environment_digest: str
    environment_key: str
    status: PackageEnvironmentStatus
    python_version: str
    sdk_version: str
    host_version: str
    host_protocol_version: str

    def __post_init__(self) -> None:
        if self.id <= 0:
            raise InvalidPackageInstallation("environment id must be positive")
        validate_sha256(self.environment_digest, field="environment digest")
        validate_storage_key(self.environment_key, field="environment key")
        if self.python_version != "3.12":
            raise InvalidPackageInstallation("G3 package environments require Python 3.12")
        if self.host_protocol_version != _HOST_PROTOCOL_VERSION:
            raise InvalidPackageInstallation("unsupported host protocol version")


@dataclass(frozen=True, slots=True)
class AgentInstancePackageBinding:
    """Owner-scoped package binding and effective validated config (ADR 0024)."""

    agent_instance_id: int
    installed_package_version_id: int
    effective_config_json: str
    effective_config_digest: str
    config_revision: int
    config_schema_digest: str

    def __post_init__(self) -> None:
        if self.agent_instance_id <= 0 or self.installed_package_version_id <= 0:
            raise InvalidPackageInstallation("binding ids must be positive")
        if self.config_revision <= 0:
            raise InvalidPackageInstallation("config_revision must be positive")
        validate_sha256(self.effective_config_digest, field="config digest")
        validate_sha256(self.config_schema_digest, field="config schema digest")
