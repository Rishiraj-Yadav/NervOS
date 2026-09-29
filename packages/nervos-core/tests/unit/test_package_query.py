"""Unit tests for PackageQueryService inspection and query mapping."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

from nervos_core.application.package_builder import PackageBuildInputs, package_build_bytes
from nervos_core.application.package_query import PackageQueryPersistence, PackageQueryService
from nervos_core.application.package_signing import Ed25519PackageSigner
from nervos_core.application.package_storage import PackageStore
from nervos_core.domain.package_installation import PackageInstallStatus
from nervos_core.domain.package_query import (
    PackageVersionSummary,
)
from nervos_core.domain.packages import InvalidPackageManifest
from package_fixtures import (
    TEST_SIGNING_SEED,
    VALID_CONFIG_SCHEMA,
    VALID_MANIFEST,
    valid_wheel,
)


def _build_test_pkg(manifest_bytes: bytes = VALID_MANIFEST) -> bytes:
    signer = Ed25519PackageSigner.from_private_bytes(TEST_SIGNING_SEED)
    inputs = PackageBuildInputs(
        manifest_bytes=manifest_bytes,
        config_schema_bytes=VALID_CONFIG_SCHEMA,
        agent_wheel_bytes=valid_wheel(),
    )
    return package_build_bytes(inputs, signer=signer)


def test_inspect_valid_package(tmp_path: Path) -> None:
    pkg_bytes = _build_test_pkg()
    pkg_file = tmp_path / "valid.nervos"
    pkg_file.write_bytes(pkg_bytes)

    store = PackageStore(tmp_path / "store")
    mock_persistence = MagicMock(spec=PackageQueryPersistence)
    service = PackageQueryService(mock_persistence, store)

    metadata = service.inspect_artifact(pkg_file)
    assert metadata.package_id == "com.acme.invoice"
    assert metadata.package_version == "1.2.3"
    assert metadata.is_compatible is True
    assert metadata.entrypoint_module == "acme_invoice.agent"
    assert metadata.entrypoint_object == "InvoiceAgent"
    assert metadata.signer_fingerprint is not None
    assert metadata.content_digest is not None


def test_inspect_incompatible_version(tmp_path: Path) -> None:
    incompat_manifest = VALID_MANIFEST.replace(
        b"min_version: 0.1.0\n  max_version: 0.1.0",
        b"min_version: 9.0.0\n  max_version: 9.9.9",
    )
    signer = Ed25519PackageSigner.from_private_bytes(TEST_SIGNING_SEED)
    inputs = PackageBuildInputs(
        manifest_bytes=incompat_manifest,
        config_schema_bytes=VALID_CONFIG_SCHEMA,
        agent_wheel_bytes=valid_wheel(),
    )
    # The G1 manifest validator correctly fails closed on incompatible version
    with pytest.raises(InvalidPackageManifest):
        package_build_bytes(inputs, signer=signer)


def test_list_packages_sorts_semver_descending(tmp_path: Path) -> None:
    store = PackageStore(tmp_path / "store")
    mock_persistence = MagicMock(spec=PackageQueryPersistence)

    v1 = PackageVersionSummary(
        package_id="com.acme.agent",
        package_version="1.0.0",
        display_name="Agent v1",
        status=PackageInstallStatus.ACTIVE,
        signer_fingerprint="fp1",
        content_digest="sha1",
        archive_digest="sha1",
        bound_instances_count=0,
        installed_at=None,
        activated_at=None,
        failed_at=None,
        removed_at=None,
        last_error_code=None,
        last_error_message=None,
    )
    v2 = PackageVersionSummary(
        package_id="com.acme.agent",
        package_version="2.0.0",
        display_name="Agent v2",
        status=PackageInstallStatus.ACTIVE,
        signer_fingerprint="fp2",
        content_digest="sha2",
        archive_digest="sha2",
        bound_instances_count=0,
        installed_at=None,
        activated_at=None,
        failed_at=None,
        removed_at=None,
        last_error_code=None,
        last_error_message=None,
    )
    v1_1 = PackageVersionSummary(
        package_id="com.acme.agent",
        package_version="1.1.0",
        display_name="Agent v1.1",
        status=PackageInstallStatus.ACTIVE,
        signer_fingerprint="fp3",
        content_digest="sha3",
        archive_digest="sha3",
        bound_instances_count=0,
        installed_at=None,
        activated_at=None,
        failed_at=None,
        removed_at=None,
        last_error_code=None,
        last_error_message=None,
    )

    mock_persistence.list_package_summaries.return_value = (v1, v2, v1_1)
    service = PackageQueryService(mock_persistence, store)

    results = service.list_packages()
    assert [r.package_version for r in results] == ["2.0.0", "1.1.0", "1.0.0"]
