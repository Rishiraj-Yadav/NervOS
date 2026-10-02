"""Deterministic, bounded `.nervos` package construction.

The production boundary is filesystem-backed. Every author input is opened once, copied in bounded
chunks into a private temporary workspace, and all validation/building uses only that immutable
snapshot. This closes the validation/write TOCTOU window without retaining all payloads in memory.
The byte-input convenience API is intentionally small-caller/test sugar: it materializes sources and
then delegates to the same snapshot/build pipeline.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from nervos_core.application.package_archive import (
    EXTRACTED_MAX_BYTES,
    MAX_ENTRIES,
    READ_CHUNK_BYTES,
    SINGLE_FILE_MAX_BYTES,
    write_canonical_archive_file,
)
from nervos_core.application.package_config_schema import parse_config_schema_json
from nervos_core.application.package_integrity import (
    INTEGRITY_FILES_PATH,
    INTEGRITY_SIGNATURE_PATH,
    ContentManifest,
    PayloadEntry,
    content_digest,
)
from nervos_core.application.package_manifest import parse_package_manifest
from nervos_core.application.package_paths import (
    UnsafePackagePath,
    canonical_order,
    canonical_package_path,
    validate_unique_package_paths,
)
from nervos_core.application.package_signing import (
    PackageSigner,
    envelope_for_verified_state,
    signature_bytes,
    signature_document,
)
from nervos_core.application.package_verification import VerifiedPackage, verify_package
from nervos_core.application.package_wheel import (
    RequirementBudget,
    WheelSource,
    agent_requires_dist,
    build_dependency_lock,
    inspect_wheel,
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


class PackageBuildError(ValueError):
    """Raised when a package cannot be built from the supplied inputs."""


class InvalidPackageLayout(ValueError):
    """Raised when package content does not match the frozen V1 archive layout."""


@dataclass(frozen=True, slots=True)
class PackageBuildSource:
    """Filesystem-backed production inputs.

    Mapping keys are canonical archive paths for assets and bare filenames for dependency wheels.
    Every value is opened exactly once while snapshotting.
    """

    manifest: Path
    config_schema: Path
    agent_wheel: Path
    readme: Path | None = None
    assets: Mapping[str, Path] | None = None
    dependency_wheels: Mapping[str, Path] | None = None


@dataclass(frozen=True, slots=True)
class PackageBuildInputs:
    """Small byte-input convenience wrapper that delegates to `PackageBuildSource`."""

    manifest_bytes: bytes
    config_schema_bytes: bytes
    agent_wheel_bytes: bytes
    readme_bytes: bytes | None = None
    assets: Mapping[str, bytes] | None = None
    dependency_wheels: Mapping[str, bytes] | None = None


@dataclass(frozen=True, slots=True)
class SnapshotMember:
    path: str
    snapshot: Path
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class BuiltPackage:
    output_path: Path
    package_id: str
    package_version: str
    content_digest: str
    archive_digest: str
    signer_fingerprint: str
    size: int
    verified: VerifiedPackage


def package_build(
    inputs: PackageBuildSource | PackageBuildInputs,
    *,
    signer: PackageSigner,
    destination: Path,
    overwrite: bool = False,
) -> BuiltPackage:
    """Snapshot, build, verify the exact temp artifact, and publish atomically."""
    if destination.exists() and not overwrite:
        raise PackageBuildError(f"refusing to overwrite existing artifact at {destination}")
    if not destination.parent.is_dir():
        raise PackageBuildError("destination parent directory does not exist")

    with tempfile.TemporaryDirectory(prefix="nervos-g2-build-") as workspace_name:
        workspace = Path(workspace_name)
        source = (
            inputs
            if isinstance(inputs, PackageBuildSource)
            else _materialize_bytes(inputs, workspace)
        )
        snapshot_dir = workspace / "snapshot"
        snapshot_dir.mkdir()
        members = _snapshot_sources(source, snapshot_dir)
        archive_members, manifest, fingerprint = _prepare_archive(members, signer, workspace)

        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".partial", dir=str(destination.parent)
        )
        os.close(handle)
        temporary = Path(temporary_name)
        try:
            write_canonical_archive_file(
                {path: member.snapshot for path, member in archive_members.items()}, temporary
            )
            verified = verify_package(temporary)
            if overwrite:
                os.replace(temporary, destination)
            else:
                try:
                    os.link(temporary, destination)
                except FileExistsError as error:
                    raise PackageBuildError(
                        f"refusing to overwrite existing artifact at {destination}"
                    ) from error
                temporary.unlink()
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    return BuiltPackage(
        output_path=destination,
        package_id=manifest.package_id,
        package_version=manifest.package_version,
        content_digest=verified.content_digest,
        archive_digest=verified.archive_digest,
        signer_fingerprint=fingerprint,
        size=destination.stat().st_size,
        verified=verified,
    )


def package_build_bytes(inputs: PackageBuildInputs, *, signer: PackageSigner) -> bytes:
    """Test/small-caller convenience; production uses `package_build` with filesystem sources."""
    with tempfile.TemporaryDirectory(prefix="nervos-g2-bytes-") as directory:
        output = Path(directory) / "package.nervos"
        package_build(inputs, signer=signer, destination=output)
        return output.read_bytes()


def _materialize_bytes(inputs: PackageBuildInputs, workspace: Path) -> PackageBuildSource:
    source_dir = workspace / "byte-sources"
    source_dir.mkdir()

    def write(name: str, payload: bytes) -> Path:
        path = source_dir / name
        path.write_bytes(payload)
        return path

    assets = {
        archive_path: write(f"asset-{index}", payload)
        for index, (archive_path, payload) in enumerate((inputs.assets or {}).items())
    }
    wheels = {
        filename: write(f"wheel-{index}.whl", payload)
        for index, (filename, payload) in enumerate((inputs.dependency_wheels or {}).items())
    }
    return PackageBuildSource(
        manifest=write("manifest.yaml", inputs.manifest_bytes),
        config_schema=write("config.schema.json", inputs.config_schema_bytes),
        agent_wheel=write("agent.whl", inputs.agent_wheel_bytes),
        readme=write("README.md", inputs.readme_bytes) if inputs.readme_bytes is not None else None,
        assets=assets,
        dependency_wheels=wheels,
    )


def _snapshot_sources(source: PackageBuildSource, directory: Path) -> dict[str, SnapshotMember]:
    paths: dict[str, Path] = {
        MANIFEST_PATH: source.manifest,
        AGENT_WHEEL_PATH: source.agent_wheel,
        CONFIG_SCHEMA_PATH: source.config_schema,
    }
    if source.readme is not None:
        paths[README_PATH] = source.readme
    for archive_path, path in (source.assets or {}).items():
        paths[archive_path] = path
    for filename, path in (source.dependency_wheels or {}).items():
        canonical_package_path(filename)
        if "/" in filename:
            raise InvalidPackageLayout("dependency wheel names must be bare filenames")
        paths[f"{WHEELS_PREFIX}{filename}"] = path

    _require_allowed_paths(paths)
    validate_unique_package_paths(paths)
    if len(paths) > MAX_ENTRIES:
        raise PackageBuildError(f"package exceeds {MAX_ENTRIES} entry limit")

    total = 0
    snapshots: dict[str, SnapshotMember] = {}
    for index, archive_path in enumerate(canonical_order(paths)):
        snapshot = directory / f"member-{index:05d}"
        member = _snapshot_one(archive_path, paths[archive_path], snapshot)
        total += member.size
        if total > EXTRACTED_MAX_BYTES:
            raise PackageBuildError(
                f"package snapshots exceed {EXTRACTED_MAX_BYTES} byte extracted limit"
            )
        snapshots[archive_path] = member
    return snapshots


def _snapshot_one(archive_path: str, source: Path, destination: Path) -> SnapshotMember:
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as reader, destination.open("xb") as writer:
            while chunk := reader.read(READ_CHUNK_BYTES):
                size += len(chunk)
                if size > SINGLE_FILE_MAX_BYTES:
                    raise PackageBuildError(
                        f"source for {archive_path!r} exceeds {SINGLE_FILE_MAX_BYTES} byte limit"
                    )
                digest.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
    except OSError as error:
        raise PackageBuildError(f"could not snapshot source for {archive_path!r}") from error
    return SnapshotMember(archive_path, destination, size, digest.hexdigest())


def _prepare_archive(
    members: dict[str, SnapshotMember],
    signer: PackageSigner,
    workspace: Path,
) -> tuple[dict[str, SnapshotMember], PackageManifest, str]:
    manifest = parse_package_manifest(members[MANIFEST_PATH].snapshot.read_bytes())
    parse_config_schema_json(members[CONFIG_SCHEMA_PATH].snapshot.read_bytes())
    if manifest.configuration.schema != CONFIG_SCHEMA_PATH:
        raise InvalidPackageLayout(f"manifest must reference {CONFIG_SCHEMA_PATH}")

    requirement_budget = RequirementBudget()
    inspect_wheel(
        members[AGENT_WHEEL_PATH].snapshot,
        filename=AGENT_WHEEL_PATH,
        requirement_budget=requirement_budget,
    )
    wheels: dict[str, WheelSource] = {
        path[len(WHEELS_PREFIX) :]: member.snapshot
        for path, member in members.items()
        if path.startswith(WHEELS_PREFIX)
    }
    lock = build_dependency_lock(wheels)
    inspected = {
        name: inspect_wheel(path, filename=name, requirement_budget=requirement_budget)
        for name, path in wheels.items()
    }
    verify_lock_against_wheelhouse(lock, inspected)
    validate_dependency_closure(
        wheels,
        extra_requires=agent_requires_dist(members[AGENT_WHEEL_PATH].snapshot),
    )
    _require_assets_match_declaration(manifest.assets.paths, members)

    allowed_nested = {AGENT_WHEEL_PATH, *(f"{WHEELS_PREFIX}{name}" for name in wheels)}
    _reject_nested_snapshot_archives(members, frozenset(allowed_nested))

    generated_dir = workspace / "generated"
    generated_dir.mkdir()
    lock_member = _write_generated(LOCK_PATH, lock.canonical_bytes(), generated_dir / "lock")
    members[LOCK_PATH] = lock_member

    content_manifest = ContentManifest(
        entries=tuple(
            PayloadEntry(path=path, sha256=members[path].sha256, size=members[path].size)
            for path in canonical_order(members)
        )
    )
    digest = content_digest(content_manifest)
    files_member = _write_generated(
        INTEGRITY_FILES_PATH,
        content_manifest.canonical_bytes(),
        generated_dir / "files",
    )
    envelope = envelope_for_verified_state(
        package_id=manifest.package_id,
        package_version=manifest.package_version,
        manifest_version=manifest.manifest_version.value,
        digest=digest,
    )
    signature_doc, fingerprint = signature_document(envelope, signer)
    signature_member = _write_generated(
        INTEGRITY_SIGNATURE_PATH,
        signature_bytes(signature_doc),
        generated_dir / "signature",
    )
    members[INTEGRITY_FILES_PATH] = files_member
    members[INTEGRITY_SIGNATURE_PATH] = signature_member
    return members, manifest, fingerprint


def _write_generated(path: str, payload: bytes, destination: Path) -> SnapshotMember:
    destination.write_bytes(payload)
    return SnapshotMember(path, destination, len(payload), hashlib.sha256(payload).hexdigest())


def _reject_nested_snapshot_archives(
    members: Mapping[str, SnapshotMember], allowed: frozenset[str]
) -> None:
    for path, member in members.items():
        if path in allowed:
            continue
        with member.snapshot.open("rb") as stream:
            prefix = stream.read(262)
        is_zip = prefix.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))
        is_tar = len(prefix) >= 262 and prefix[257:262] == b"ustar"
        if is_zip or is_tar:
            raise InvalidPackageLayout(f"undeclared nested archive at {path!r}")


def _require_assets_match_declaration(
    declared: tuple[str, ...], members: Mapping[str, SnapshotMember]
) -> None:
    present = {path for path in members if path.startswith("assets/")}
    if set(declared) != present:
        missing = sorted(set(declared) - present)
        extra = sorted(present - set(declared))
        raise InvalidPackageLayout(
            f"assets must match the manifest declaration (missing={missing}, undeclared={extra})"
        )
    if any(not path.startswith("assets/") for path in declared):
        raise InvalidPackageLayout("declared assets must live under assets/")


def _require_allowed_paths(paths: Mapping[str, Path]) -> None:
    for path in paths:
        try:
            canonical_package_path(path)
        except UnsafePackagePath as error:
            raise InvalidPackageLayout(f"unsafe archive path: {error}") from error
        if path.split("/", 1)[0] not in ALLOWED_TOP_LEVEL:
            raise InvalidPackageLayout(f"archive member {path!r} is outside the V1 layout")
