"""Secure `.nervos` package verification.

The pipeline is ordered so cheap structural rejections happen before expensive cryptographic work,
and every step fails closed. Nothing in this module executes package code: a `.nervos` artifact can
be fully validated without a single line of the agent running, which is the property that makes it
safe to inspect an untrusted package at all.

Verification establishes **integrity** (the bytes are what the manifest says) and **authenticity**
(an Ed25519 signature verifies with the embedded key). It deliberately establishes no **trust**: a
verified package from an unknown signer is reported as exactly that, and the operator decides.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from nervos_core.application.package_archive import (
    ArchiveValidationProfile,
    BoundedArchiveReader,
    PackageArchiveError,
    reject_undeclared_nested_archives,
)
from nervos_core.application.package_config_schema import (
    PackageConfigSchema,
    parse_config_schema_json,
)
from nervos_core.application.package_integrity import (
    INTEGRITY_FILES_PATH,
    INTEGRITY_SIGNATURE_PATH,
    NON_PAYLOAD_PATHS,
    AcceptedFileEntry,
    MalformedIntegrityManifest,
    content_digest,
    parse_content_manifest,
    sha256_hex,
    verify_payload_against_manifest,
)
from nervos_core.application.package_manifest import parse_package_manifest
from nervos_core.application.package_paths import (
    canonical_package_path,
)
from nervos_core.application.package_signing import (
    envelope_for_verified_state,
    parse_signature_document,
    verify_package_signature,
)
from nervos_core.application.package_wheel import (
    DependencyLock,
    WheelMetadata,
    agent_requires_dist,
    inspect_wheel,
    parse_dependency_lock,
    validate_dependency_closure,
    verify_lock_against_wheelhouse,
)
from nervos_core.domain.packages import PackageManifest

MANIFEST_PATH = "manifest.yaml"
AGENT_WHEEL_PATH = "agent.whl"
CONFIG_SCHEMA_PATH = "config.schema.json"
README_PATH = "README.md"
LOCK_PATH = "dependencies/lock.json"
WHEELS_PREFIX = "dependencies/wheels/"

ALLOWED_TOP_LEVEL = frozenset(
    {
        "manifest.yaml",
        "agent.whl",
        "config.schema.json",
        "README.md",
        "assets",
        "dependencies",
        "integrity",
    }
)

_REQUIRED_MEMBERS = (MANIFEST_PATH, AGENT_WHEEL_PATH, CONFIG_SCHEMA_PATH, LOCK_PATH)
_REQUIRED_INTEGRITY = (INTEGRITY_FILES_PATH, INTEGRITY_SIGNATURE_PATH)


class InvalidPackageLayout(ValueError):
    """Raised when an archive's members do not match the frozen V1 layout."""


class PackageVerificationError(ValueError):
    """Raised when a package cannot be verified."""


@dataclass(frozen=True, slots=True)
class VerifiedPackage:
    """Everything G3 receives about a package, and the only way verified facts leave G2.

    Immutable by construction. It carries no installation path, no database id, no environment
    location, and no AgentInstance reference: G2 has no installation state, and adding any here
    would blur the boundary G3 is defined by.
    """

    manifest: PackageManifest
    config_schema: PackageConfigSchema
    content_digest: str
    archive_digest: str
    signer_public_key: bytes
    signer_fingerprint: str
    dependency_lock: DependencyLock
    agent_wheel: WheelMetadata
    dependencies: tuple[WheelMetadata, ...]
    entries: tuple[AcceptedFileEntry, ...]


def verify_package(
    path: Path | None = None,
    *,
    source: BinaryIO | None = None,
    archive_bytes: bytes | None = None,
) -> VerifiedPackage:
    """Verify a package from a path/seekable source without buffering the outer archive.

    `archive_bytes` remains a convenience for small tests and programmatic callers; it delegates to
    the same `BoundedArchiveReader` rules.
    """
    selected: Path | BinaryIO | bytes
    if path is not None:
        selected = path
    elif source is not None:
        selected = source
    elif archive_bytes is not None:
        selected = archive_bytes
    else:
        raise PackageVerificationError("verification needs an archive path, source, or bytes")

    reader = BoundedArchiveReader(selected, profile=ArchiveValidationProfile.NERVOS_V1)
    paths = reader.paths()
    _require_layout(paths)

    # Read only what is needed, each member bounded and size-checked while streaming.
    manifest_bytes = reader.read(MANIFEST_PATH)
    config_bytes = reader.read(CONFIG_SCHEMA_PATH)
    agent_wheel_bytes = reader.read(AGENT_WHEEL_PATH)
    lock_bytes = reader.read(LOCK_PATH)
    manifest_document_bytes = reader.read(INTEGRITY_FILES_PATH)
    signature_bytes = reader.read(INTEGRITY_SIGNATURE_PATH)

    payload_paths = tuple(path for path in paths if path not in NON_PAYLOAD_PATHS)
    payloads = {path: reader.read(path) for path in payload_paths}
    _reject_undeclared_nested_archives(payloads)

    # G1 remains the single authority for manifest and configuration validation.
    manifest = parse_package_manifest(manifest_bytes)
    config_schema = parse_config_schema_json(config_bytes)

    agent_wheel = inspect_wheel(agent_wheel_bytes, filename=AGENT_WHEEL_PATH)
    lock = parse_dependency_lock(lock_bytes)

    wheel_payloads = {
        path[len(WHEELS_PREFIX) :]: payloads[path]
        for path in payload_paths
        if path.startswith(WHEELS_PREFIX)
    }
    dependencies = tuple(
        inspect_wheel(payload, filename=filename)
        for filename, payload in sorted(
            wheel_payloads.items(), key=lambda kv: kv[0].encode("utf-8")
        )
    )
    verify_lock_against_wheelhouse(lock, {wheel.filename: wheel for wheel in dependencies})
    validate_dependency_closure(
        wheel_payloads, extra_requires=agent_requires_dist(agent_wheel_bytes)
    )

    _require_assets_match_declaration(manifest.assets.paths, payload_paths)

    # Recompute every digest from the bytes that were actually read, then require the declared
    # manifest to describe exactly that set.
    manifest_document = parse_content_manifest(manifest_document_bytes)
    verify_payload_against_manifest(manifest_document, payloads)
    if any(entry.path in NON_PAYLOAD_PATHS for entry in manifest_document.entries):
        raise MalformedIntegrityManifest("payload manifest must not list integrity members")

    digest = content_digest(manifest_document)

    envelope = parse_signature_document(signature_bytes)
    expected_envelope = envelope_for_verified_state(
        package_id=manifest.package_id,
        package_version=manifest.package_version,
        manifest_version=manifest.manifest_version.value,
        digest=digest,
    )
    fingerprint = verify_package_signature(envelope, expected_envelope=expected_envelope)

    return VerifiedPackage(
        manifest=manifest,
        config_schema=config_schema,
        content_digest=digest,
        archive_digest=_archive_digest(selected),
        signer_public_key=envelope.public_key,
        signer_fingerprint=fingerprint,
        dependency_lock=lock,
        agent_wheel=agent_wheel,
        dependencies=dependencies,
        entries=manifest_document.entries,
    )


def _archive_digest(source: Path | BinaryIO | bytes) -> str:
    if isinstance(source, bytes):
        return sha256_hex(source)
    digest = hashlib.sha256()
    if isinstance(source, Path):
        stream = source.open("rb")
        should_close = True
    else:
        stream = source
        should_close = False
        stream.seek(0)
    try:
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
    finally:
        if should_close:
            stream.close()
    return digest.hexdigest()


def _require_layout(paths: tuple[str, ...]) -> None:
    """Require the frozen V1 layout: required members present, everything inside the allowlist.

    Path safety is re-asserted here through the *shared* validator rather than only inside the
    archive reader, so the verifier's layout rule and the builder's rule cannot drift apart.
    """
    for path in paths:
        canonical_package_path(path)
        top = path.split("/", 1)[0]
        if top not in ALLOWED_TOP_LEVEL:
            raise InvalidPackageLayout(f"archive member {path!r} is outside the V1 layout")

    present = set(paths)
    for required in _REQUIRED_MEMBERS:
        if required not in present:
            raise InvalidPackageLayout(f"archive is missing required member {required!r}")
    for required in _REQUIRED_INTEGRITY:
        if required not in present:
            raise InvalidPackageLayout(f"archive is missing required member {required!r}")

    # `dependencies/` and `integrity/` must contain nothing but their frozen members.
    for path in paths:
        if (
            path.startswith("dependencies/")
            and path != LOCK_PATH
            and not path.startswith(WHEELS_PREFIX)
        ):
            raise InvalidPackageLayout(f"unexpected member under dependencies/: {path!r}")
        if path.startswith(WHEELS_PREFIX) and not path.endswith(".whl"):
            raise InvalidPackageLayout("dependencies/wheels/ may contain only wheel files")
        if path.startswith("integrity/") and path not in _REQUIRED_INTEGRITY:
            raise InvalidPackageLayout(f"unexpected member under integrity/: {path!r}")


def _reject_undeclared_nested_archives(payloads: Mapping[str, bytes]) -> None:
    """Delegate nested-content classification to the shared archive rule."""
    allowed = {AGENT_WHEEL_PATH}
    allowed.update(path for path in payloads if path.startswith(WHEELS_PREFIX))
    try:
        reject_undeclared_nested_archives(payloads, allowed_paths=frozenset(allowed))
    except PackageArchiveError as error:
        raise InvalidPackageLayout(str(error)) from error


def _require_assets_match_declaration(
    declared: tuple[str, ...], payload_paths: tuple[str, ...]
) -> None:
    """The manifest declares assets and the archive carries exactly them, nothing more or less.

    G1 declares these archive-relative (`assets/logo.png`), so the comparison is direct.
    """
    present_members = {path for path in payload_paths if path.startswith("assets/")}
    if set(declared) != present_members:
        missing = sorted(set(declared) - present_members)
        undeclared = sorted(present_members - set(declared))
        raise InvalidPackageLayout(
            "assets must match the manifest declaration "
            f"(missing={missing}, undeclared={undeclared})"
        )
