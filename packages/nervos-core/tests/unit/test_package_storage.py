"""Unit tests for G3 same-volume package storage, immutable snapshotting, and TOCTOU protection."""

# pyright: basic

from __future__ import annotations

import io
from pathlib import Path

import pytest
from nervos_core.application.package_builder import PackageBuildInputs, package_build_bytes
from nervos_core.application.package_signing import Ed25519PackageSigner
from nervos_core.application.package_storage import PackageStore, PackageStorageError
from package_fixtures import TEST_SIGNING_SEED, VALID_CONFIG_SCHEMA, VALID_MANIFEST, valid_wheel


def _signer() -> Ed25519PackageSigner:
    return Ed25519PackageSigner.from_private_bytes(TEST_SIGNING_SEED)


def _built_bytes() -> bytes:
    return package_build_bytes(
        PackageBuildInputs(
            manifest_bytes=VALID_MANIFEST,
            config_schema_bytes=VALID_CONFIG_SCHEMA,
            agent_wheel_bytes=valid_wheel(),
        ),
        signer=_signer(),
    )


def test_package_store_snapshots_before_verification(tmp_path: Path) -> None:
    store = PackageStore(tmp_path / "store")
    raw = _built_bytes()
    source_file = tmp_path / "caller_pkg.nervos"
    source_file.write_bytes(raw)

    staged = store.snapshot_and_verify(source_file)
    assert staged.archive.is_file()
    assert staged.verified.manifest.package_id == "com.acme.invoice"
    assert staged.archive_digest == staged.verified.archive_digest

    store.cleanup_staging(staged)
    assert not staged.directory.exists()


def test_package_store_toctou_mutation_protection(tmp_path: Path) -> None:
    """Mutating caller-owned file after staging has zero effect on materialized payload."""
    store = PackageStore(tmp_path / "store")
    raw = _built_bytes()
    source_file = tmp_path / "caller_pkg.nervos"
    source_file.write_bytes(raw)

    staged = store.snapshot_and_verify(source_file)

    # Malicious actor tampers with caller source file after verification
    source_file.write_bytes(b"corrupted or hostile content")

    # Materialization reads ONLY from the immutable staged snapshot
    payload_dir = store.materialize(staged)
    assert (payload_dir / "manifest.yaml").is_file()
    assert (payload_dir / "agent.whl").is_file()

    key, dest = store.publish_payload(staged, payload_dir)
    assert dest.is_dir()
    assert (dest / "manifest.yaml").read_bytes() == VALID_MANIFEST

    store.cleanup_staging(staged)


def test_materialization_validates_staged_hashes(tmp_path: Path) -> None:
    store = PackageStore(tmp_path / "store")
    staged = store.snapshot_and_verify(io.BytesIO(_built_bytes()))
    payload = store.materialize(staged)
    assert (payload / "manifest.yaml").is_file()
    store.cleanup_staging(staged)


def test_orphan_staging_cleanup(tmp_path: Path) -> None:
    store = PackageStore(tmp_path / "store")
    store.prepare()
    debris_dir = store.staging_root / "install-orphan-123"
    debris_dir.mkdir()
    (debris_dir / "leftover.bin").write_bytes(b"debris")

    active_dir = store.staging_root / "install-active-456"
    active_dir.mkdir()

    cleaned = store.reconcile_orphan_staging(
        durable_operation_ids=frozenset({"install-active-456"})
    )
    assert cleaned == ("install-orphan-123",)
    assert not debris_dir.exists()
    assert active_dir.exists()
