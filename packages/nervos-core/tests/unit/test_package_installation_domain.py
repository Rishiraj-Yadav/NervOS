"""Unit tests for G3 package installation domain values, authorization, and snapshots."""

# pyright: basic

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from nervos_core.domain.execution import (
    BUILTIN_EXECUTABLE_SNAPSHOT,
    RunExecutableSnapshot,
    RunExecutionKind,
)
from nervos_core.domain.package_installation import (
    AgentInstancePackageBinding,
    InstalledPackageVersion,
    InvalidPackageInstallation,
    PackageEnvironment,
    PackageEnvironmentStatus,
    PackageInstallAuthorization,
    PackageInstallStatus,
    validate_package_status,
    validate_safe_package_error,
    validate_sha256,
    validate_storage_key,
)


def _now() -> datetime:
    return datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)


def test_package_install_authorization_validation() -> None:
    auth = PackageInstallAuthorization(
        package_id="com.acme.invoice",
        package_version="1.2.3",
        content_digest="a" * 64,
        signer_fingerprint="b" * 64,
        approved_by_user_id=1,
        approved_at=_now(),
        archive_digest="c" * 64,
    )
    assert auth.package_id == "com.acme.invoice"
    assert auth.package_version == "1.2.3"

    with pytest.raises(ValueError):
        PackageInstallAuthorization(
            package_id="nervos.invalid",
            package_version="1.0.0",
            content_digest="a" * 64,
            signer_fingerprint="b" * 64,
            approved_by_user_id=1,
            approved_at=_now(),
        )

    with pytest.raises(InvalidPackageInstallation):
        PackageInstallAuthorization(
            package_id="com.acme.invoice",
            package_version="1.2.3",
            content_digest="short",
            signer_fingerprint="b" * 64,
            approved_by_user_id=1,
            approved_at=_now(),
        )


def test_installed_package_version_validation() -> None:
    version = InstalledPackageVersion(
        id=1,
        package_id="com.acme.invoice",
        package_version="1.2.3",
        status=PackageInstallStatus.ACTIVE,
        content_digest="a" * 64,
        archive_digest="b" * 64,
        signer_fingerprint="c" * 64,
        storage_key="packages/com_acme_invoice/1_2_3/digest",
        environment_id=1,
        created_at=_now(),
        updated_at=_now(),
    )
    assert version.id == 1
    assert version.status is PackageInstallStatus.ACTIVE

    with pytest.raises(InvalidPackageInstallation):
        InstalledPackageVersion(
            id=0,
            package_id="com.acme.invoice",
            package_version="1.2.3",
            status=PackageInstallStatus.ACTIVE,
            content_digest="a" * 64,
            archive_digest="b" * 64,
            signer_fingerprint="c" * 64,
            storage_key=None,
            environment_id=None,
            created_at=_now(),
            updated_at=_now(),
        )


def test_package_environment_validation() -> None:
    env = PackageEnvironment(
        id=1,
        environment_digest="e" * 64,
        environment_key="environments/" + "e" * 64,
        status=PackageEnvironmentStatus.READY,
        python_version="3.12",
        sdk_version="0.1.0",
        host_version="0.1.0",
        host_protocol_version="1",
    )
    assert env.python_version == "3.12"
    assert env.status is PackageEnvironmentStatus.READY

    with pytest.raises(InvalidPackageInstallation):
        PackageEnvironment(
            id=1,
            environment_digest="e" * 64,
            environment_key="environments/e",
            status=PackageEnvironmentStatus.READY,
            python_version="3.11",
            sdk_version="0.1.0",
            host_version="0.1.0",
            host_protocol_version="1",
        )


def test_run_executable_snapshot_builtin() -> None:
    builtin = BUILTIN_EXECUTABLE_SNAPSHOT
    assert builtin.execution_kind == RunExecutionKind.BUILTIN
    assert builtin.installed_package_version_id is None
    assert builtin.effective_config_json == "{}"


def test_storage_key_validation() -> None:
    assert validate_storage_key("packages/acme/1_0_0/abc") == "packages/acme/1_0_0/abc"
    with pytest.raises(InvalidPackageInstallation):
        validate_storage_key("/leading/slash")
    with pytest.raises(InvalidPackageInstallation):
        validate_storage_key("traversal/../escape")
    with pytest.raises(InvalidPackageInstallation):
        validate_storage_key("trailing/slash/")


def test_safe_package_error_validation() -> None:
    code, msg = validate_safe_package_error("install_failed", "Safe error message")
    assert code == "install_failed"
    assert msg == "Safe error message"

    with pytest.raises(InvalidPackageInstallation):
        validate_safe_package_error("install_failed", None)
