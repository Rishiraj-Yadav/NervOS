"""Wheel metadata inspection, the dependency lock, and offline dependency-closure validation.

Everything here reads metadata as **bytes**. A wheel is a ZIP archive, so it is opened with the same
bounded reader the outer archive uses and its members are treated as untrusted paths; nothing is
imported, extracted to disk, or executed. This is the load-bearing property of the whole milestone:
a `.nervos` package can be fully inspected and verified without a single line of package code
running.

Two distinct identity systems meet in this module and are kept apart on purpose:

* **NervOS package identity** is the frozen reverse-DNS grammar from ADR 0024, strict SemVer, and
  validation-only. It is never derived from, or required to equal, a Python distribution name.
* **Python distribution identity** is PEP 503/PEP 440. It applies to dependency wheels and to the
  agent wheel's `METADATA`, and `packaging` is the authority for normalizing it.

Conflating them would be a quiet format decision, so they are separate functions with separate
normalizers.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import InvalidWheelFilename, parse_wheel_filename
from packaging.version import InvalidVersion, Version

from nervos_core.application.package_archive import (
    ArchiveValidationProfile,
    BoundedArchiveReader,
    PackageArchiveError,
)
from nervos_core.application.package_integrity import (
    MalformedIntegrityManifest,
    canonical_json_bytes,
    normalized_distribution_name,
    normalized_distribution_version,
    sha256_hex,
)
from nervos_core.application.package_paths import (
    PackageSizeLimitExceeded,
    UnsafePackagePath,
    canonical_package_path,
)
from nervos_core.application.package_wheel_metadata import (
    MetadataValueLimitExceeded,
    selected_headers,
)
from nervos_core.application.package_wheel_version import normalized_metadata_version

# V1 is pure-Python only (Stage-G §22/§29, ADR 0026): `py3-none-any` and nothing else.
REQUIRED_WHEEL_TAG = "py3-none-any"
LOCK_FORMAT_VERSION = "1"
type WheelSource = bytes | Path

# G2 V1 targets exactly Python 3.12. Future Python minors require reviewed compatibility expansion.
_TARGET_PYTHON_VERSION = "3.12"

_METADATA_MEMBER_SUFFIX = ".dist-info/METADATA"
_WHEEL_MEMBER_SUFFIX = ".dist-info/WHEEL"
_DIST_INFO_MARKER = ".dist-info/"

# Conservative subset of packaging's IDENTIFIER grammar. Only a bare ASCII
# alphanumeric name followed immediately by a marker qualifies. Extras, URLs,
# specifiers, folding and all other spellings keep the complete parser path.
_BARE_MARKED_REQUIREMENT = re.compile(r"[A-Za-z0-9]+([ \t]*;[^\r\n]*)\Z")

# User-approved bounded metadata policy; ADR 0031. These apply to every
# occurrence, including duplicates and inactive markers, across a whole package.
MAX_REQUIREMENT_VALUE_BYTES = 64 * 1024
MAX_REQUIREMENT_TOTAL_BYTES = 4 * 1024 * 1024
MAX_REQUIREMENT_OCCURRENCES = 100_000


@dataclass(slots=True)
class RequirementBudget:
    """One inspection pass's package-wide Requires-Dist admission budget."""

    bytes_used: int = 0
    occurrences: int = 0

    def consume(self, value: str) -> None:
        size = len(str(value).encode("utf-8", "surrogatepass"))
        if size > MAX_REQUIREMENT_VALUE_BYTES:
            raise InvalidDependencyWheel("Requires-Dist logical value budget exceeded")
        self.bytes_used += size
        self.occurrences += 1
        if self.bytes_used > MAX_REQUIREMENT_TOTAL_BYTES:
            raise InvalidDependencyWheel("Requires-Dist package byte budget exceeded")
        if self.occurrences > MAX_REQUIREMENT_OCCURRENCES:
            raise InvalidDependencyWheel("Requires-Dist package occurrence budget exceeded")


class InvalidAgentWheel(ValueError):
    """Raised when `agent.whl` is not a valid V1 pure-Python wheel."""


class InvalidDependencyWheel(ValueError):
    """Raised when a dependency wheel fails V1 packaging rules."""


class InvalidDependencyLock(ValueError):
    """Raised when `dependencies/lock.json` is malformed or inconsistent with the wheelhouse."""


class DependencyClosureError(ValueError):
    """Raised when the wheelhouse cannot satisfy a declared dependency offline."""


@dataclass(frozen=True, slots=True)
class WheelMetadata:
    """Verified metadata read from a wheel without importing it."""

    filename: str
    name: str
    version: str
    tag: str
    requires_python: str | None
    requires_dist: tuple[str, ...]
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class LockEntry:
    """One pinned dependency artifact."""

    name: str
    version: str
    wheel: str
    sha256: str
    size: int

    def as_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "sha256": self.sha256,
            "size": self.size,
            "version": self.version,
            "wheel": self.wheel,
        }


@dataclass(frozen=True, slots=True)
class DependencyLock:
    """The canonical `dependencies/lock.json` value."""

    entries: tuple[LockEntry, ...]

    def as_json(self) -> dict[str, object]:
        return {
            "dependencies": [entry.as_json() for entry in self.entries],
            "lock_format_version": LOCK_FORMAT_VERSION,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_json())

    def by_name(self) -> dict[str, LockEntry]:
        return {entry.name: entry for entry in self.entries}


def _split_wheel_member(paths: Iterable[str], suffix: str) -> list[str]:
    return sorted(path for path in paths if path.endswith(suffix))


def _metadata_headers(
    chunks: Iterable[bytes], *, requirement_budget: RequirementBudget | None = None
) -> dict[str, list[str]]:
    """Retain consumed evidence; duplicate single-use fields need only two values."""
    collected: dict[str, list[str]] = {}
    budget = requirement_budget if requirement_budget is not None else RequirementBudget()
    for key, value in _bounded_wheel_headers(
        chunks, frozenset({"name", "version", "requires-python", "requires-dist"}), budget
    ):
        values = collected.setdefault(key, [])
        if key == "requires-dist" or len(values) < 2:
            values.append(value)
    return collected


def _bounded_wheel_headers(
    chunks: Iterable[bytes], selected: frozenset[str], budget: RequirementBudget
) -> Iterator[tuple[str, str]]:
    try:
        for key, value in selected_headers(
            chunks, selected, value_limits={"requires-dist": MAX_REQUIREMENT_VALUE_BYTES}
        ):
            if key == "requires-dist":
                budget.consume(value)
            yield key, value
    except MetadataValueLimitExceeded as error:
        raise InvalidDependencyWheel(str(error)) from error


def inspect_wheel(
    raw: WheelSource, *, filename: str, requirement_budget: RequirementBudget | None = None
) -> WheelMetadata:
    """Inspect one wheel's metadata as bytes. Never imports, never extracts.

    The filename is *not* parsed as a standard wheel filename. A wheel's real identity is its
    internal `METADATA`; trusting the filename would mean trusting attacker-controlled text over the
    artifact's own declaration, and the frozen rules are stronger if they are read from inside.
    """
    if not filename.endswith(".whl"):
        raise InvalidDependencyWheel("dependency artifacts must be wheel files")
    canonical_package_path(filename)

    try:
        archive = BoundedArchiveReader(raw, profile=ArchiveValidationProfile.WHEEL)
    except (PackageArchiveError, UnsafePackagePath, PackageSizeLimitExceeded) as error:
        raise InvalidDependencyWheel("wheel is not a valid ZIP archive") from error

    paths = archive.paths()
    dist_info_dirs = {
        path.split(_DIST_INFO_MARKER, 1)[0] + _DIST_INFO_MARKER
        for path in paths
        if _DIST_INFO_MARKER in path
    }
    if len(dist_info_dirs) != 1:
        raise InvalidDependencyWheel("wheel must contain exactly one .dist-info directory")

    metadata_paths = _split_wheel_member(paths, _METADATA_MEMBER_SUFFIX)
    if len(metadata_paths) != 1:
        raise InvalidDependencyWheel("wheel must contain exactly one METADATA")
    wheel_paths = _split_wheel_member(paths, _WHEEL_MEMBER_SUFFIX)
    if len(wheel_paths) != 1:
        raise InvalidDependencyWheel("wheel must contain exactly one WHEEL")

    tag_seen, tags_valid = False, True
    root_is_purelib: list[str] = []
    for key, value in selected_headers(
        archive.stream(wheel_paths[0]), frozenset({"tag", "root-is-purelib"})
    ):
        if key == "tag":
            tag_seen = True
            tags_valid = tags_valid and value == REQUIRED_WHEEL_TAG
        elif len(root_is_purelib) < 2:
            root_is_purelib.append(value)
    if not tag_seen or not tags_valid:
        raise InvalidDependencyWheel(f"V1 requires every wheel tag to be {REQUIRED_WHEEL_TAG}")

    if root_is_purelib != ["true"]:
        raise InvalidDependencyWheel("V1 requires Root-Is-Purelib: true")

    metadata = _metadata_headers(
        archive.stream(metadata_paths[0]), requirement_budget=requirement_budget
    )
    declared_name = metadata.get("name", [])
    declared_version = metadata.get("version", [])
    declared_requires_python = metadata.get("requires-python", [])
    if len(declared_name) != 1 or len(declared_version) != 1:
        raise InvalidDependencyWheel("wheel METADATA must declare Name and Version exactly once")
    if len(declared_requires_python) > 1:
        raise InvalidDependencyWheel("wheel METADATA may declare Requires-Python at most once")
    normalized_name = normalized_distribution_name(declared_name[0])
    normalized_version = _require_pep440(declared_version[0])
    requires_python = declared_requires_python[0] if declared_requires_python else None
    _validate_requires_python(requires_python)
    if filename != "agent.whl":
        _validate_dependency_filename(
            filename,
            expected_name=normalized_name,
            expected_version=normalized_version,
        )

    return WheelMetadata(
        filename=filename,
        name=normalized_name,
        version=normalized_version,
        tag=REQUIRED_WHEEL_TAG,
        requires_python=requires_python,
        requires_dist=tuple(metadata.get("requires-dist", ())),
        sha256=_source_sha256(raw),
        size=_source_size(raw),
    )


def _validate_dependency_filename(
    filename: str,
    *,
    expected_name: str,
    expected_version: str,
) -> None:
    try:
        name, version, _build, tags = parse_wheel_filename(filename)
    except InvalidWheelFilename as error:
        raise InvalidDependencyWheel("dependency wheel filename is malformed") from error
    if normalized_distribution_name(name) != expected_name:
        raise InvalidDependencyWheel("dependency wheel filename name does not match METADATA")
    if str(version) != expected_version:
        raise InvalidDependencyWheel("dependency wheel filename version does not match METADATA")
    if not tags or any(str(tag) != REQUIRED_WHEEL_TAG for tag in tags):
        raise InvalidDependencyWheel("dependency wheel filename is not py3-none-any")


def _source_size(source: WheelSource) -> int:
    return source.stat().st_size if isinstance(source, Path) else len(source)


def _source_sha256(source: WheelSource) -> str:
    if isinstance(source, bytes):
        return sha256_hex(source)
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _require_pep440(value: str) -> str:
    try:
        if type(value) is str and len(value) > 65536:
            return normalized_metadata_version(value)
        return normalized_distribution_version(value)
    except (MalformedIntegrityManifest, InvalidVersion) as error:
        raise InvalidDependencyWheel("wheel version is not valid PEP 440") from error


def _validate_requires_python(value: str | None) -> None:
    """Require a wheel to support every Python interpreter in the V1 target set.

    V1 is frozen as Python 3.12+ with pure-Python wheels. A wheel that excludes one target version
    is not portable across the supported V1 runtime, even when its tags are `py3-none-any`.
    """
    if value is None:
        return
    if not value:
        raise InvalidDependencyWheel("wheel Requires-Python must not be empty")
    if type(value) is str and len(value) > 65536:
        _validate_large_requires_python(value)
        return
    try:
        specifier = SpecifierSet(value)
    except InvalidSpecifier as error:
        raise InvalidDependencyWheel("wheel Requires-Python is not a valid specifier") from error
    if Version(_TARGET_PYTHON_VERSION) not in specifier:
        raise InvalidDependencyWheel(
            f"wheel Requires-Python does not support the V1 target Python {_TARGET_PYTHON_VERSION}"
        )


def _validate_large_requires_python(value: str) -> None:
    """Same comma-separated SpecifierSet conjunction without an attacker-sized split list.

    Validate every clause before reporting incompatibility, preserving the old
    invalid-syntax precedence. Empty comma-separated clauses remain ignored.
    The frozen target is a final version, so prerelease inference cannot change
    its membership in the conjunction.
    """
    target = Version(_TARGET_PYTHON_VERSION)
    compatible = True
    cached: dict[str, bool] = {}
    for match in re.finditer(r"[^,]+", value):
        clause = match.group().strip()
        if not clause:
            continue
        if clause in cached:
            supported = cached[clause]
        else:
            try:
                supported = target in SpecifierSet(clause)
            except InvalidSpecifier as error:
                raise InvalidDependencyWheel(
                    "wheel Requires-Python is not a valid specifier"
                ) from error
            if len(cached) < 128 and len(clause) <= 1024:
                cached[clause] = supported
        compatible = compatible and supported
    if not compatible:
        raise InvalidDependencyWheel(
            f"wheel Requires-Python does not support the V1 target Python {_TARGET_PYTHON_VERSION}"
        )


def inspect_wheelhouse(wheels: Mapping[str, WheelSource]) -> tuple[WheelMetadata, ...]:
    """Inspect every dependency wheel and require the V1 pure-Python policy across the set."""
    inspected: list[WheelMetadata] = []
    for filename in sorted(wheels, key=lambda item: item.encode("utf-8")):
        inspected.append(inspect_wheel(wheels[filename], filename=filename))
    return tuple(inspected)


def build_dependency_lock(wheels: Mapping[str, WheelSource]) -> DependencyLock:
    """Derive the canonical lock from the supplied wheel set.

    The builder *regenerates* the lock rather than trusting author-supplied lock bytes: the lock is
    NervOS-generated metadata (which the frozen reproducibility rule permits canonicalizing), and
    deriving it from the artifacts means the lock cannot disagree with what is actually shipped.
    """
    inspected = inspect_wheelhouse(wheels)
    entries = [
        LockEntry(
            name=wheel.name,
            version=wheel.version,
            wheel=wheel.filename,
            sha256=wheel.sha256,
            size=wheel.size,
        )
        for wheel in inspected
    ]
    _require_unique_lock_entries(entries)
    return DependencyLock(entries=tuple(entries))


def _require_unique_lock_entries(entries: Iterable[LockEntry]) -> None:
    seen_name: dict[str, str] = {}
    seen_file: dict[str, str] = {}
    for entry in entries:
        if entry.name in seen_name:
            raise InvalidDependencyLock("lock must not pin two versions of one distribution")
        if entry.wheel in seen_file:
            raise InvalidDependencyLock("lock must not reference one artifact twice")
        seen_name[entry.name] = entry.version
        seen_file[entry.wheel] = entry.name


def parse_dependency_lock(raw: bytes) -> DependencyLock:
    """Parse and validate `dependencies/lock.json`, requiring canonical form."""
    try:
        parsed_document: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidDependencyLock("lock must be valid UTF-8 JSON") from error
    if not isinstance(parsed_document, dict):
        raise InvalidDependencyLock("lock root must be an object")
    document = cast("dict[str, object]", parsed_document)
    if document.get("lock_format_version") != LOCK_FORMAT_VERSION:
        raise InvalidDependencyLock("unsupported lock_format_version")
    declared_entries = document.get("dependencies")
    if not isinstance(declared_entries, list):
        raise InvalidDependencyLock("lock must list dependencies")

    entries: list[LockEntry] = []
    for raw_entry in cast("list[object]", declared_entries):
        if not isinstance(raw_entry, dict):
            raise InvalidDependencyLock("lock entries must be objects")
        entry = cast("dict[str, object]", raw_entry)
        name = entry.get("name")
        version = entry.get("version")
        wheel = entry.get("wheel")
        digest = entry.get("sha256")
        size = entry.get("size")
        if (
            not isinstance(name, str)
            or not isinstance(version, str)
            or not isinstance(wheel, str)
            or not isinstance(digest, str)
        ):
            raise InvalidDependencyLock("lock entries must carry name, version, wheel, sha256")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise InvalidDependencyLock("lock entry sizes must be non-negative integers")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise InvalidDependencyLock("lock entry digests must be lowercase SHA-256 hex")
        canonical_package_path(wheel)
        entries.append(
            LockEntry(
                name=normalized_distribution_name(name),
                version=_require_lock_version(version),
                wheel=wheel,
                sha256=digest,
                size=size,
            )
        )

    lock = DependencyLock(entries=tuple(entries))
    _require_unique_lock_entries(lock.entries)
    if lock.canonical_bytes() != raw:
        raise InvalidDependencyLock("lock is not canonical")
    return lock


def _require_lock_version(value: str) -> str:
    try:
        return normalized_distribution_version(value)
    except MalformedIntegrityManifest as error:
        raise InvalidDependencyLock("lock entry version is not valid PEP 440") from error


def verify_lock_against_wheelhouse(
    lock: DependencyLock,
    wheels: Mapping[str, WheelMetadata],
) -> None:
    """Require an exact, complete, bidirectional match between lock and wheelhouse."""
    pinned = lock.by_name()
    available = {wheel.name: wheel for wheel in wheels.values()}
    if set(pinned) != set(available):
        missing = sorted(set(pinned) - set(available))
        extra = sorted(set(available) - set(pinned))
        raise InvalidDependencyLock(
            f"lock and wheelhouse disagree (missing wheels={missing}, unpinned wheels={extra})"
        )
    for name, entry in pinned.items():
        wheel = available[name]
        if wheel.version != entry.version:
            raise InvalidDependencyLock(f"locked version for {name!r} does not match its wheel")
        if wheel.filename != entry.wheel:
            raise InvalidDependencyLock(f"locked filename for {name!r} does not match its wheel")
        if wheel.sha256 != entry.sha256 or wheel.size != entry.size:
            raise InvalidDependencyLock(f"locked digest for {name!r} does not match its wheel")
        for other_filename, other in wheels.items():
            if other.name == name and other_filename != entry.wheel:
                raise InvalidDependencyLock("lock must reference exactly one artifact per version")


def build_wheelhouse_index(
    wheels: Mapping[str, WheelSource],
) -> tuple[dict[str, bytes], dict[str, str]]:
    """Map canonical wheel filenames to bytes and normalized distribution names to filenames."""
    by_filename: dict[str, bytes] = {}
    by_name: dict[str, str] = {}
    for filename, source in sorted(wheels.items(), key=lambda item: item[0].encode("utf-8")):
        wheel = inspect_wheel(source, filename=filename)
        if wheel.name in by_name:
            raise InvalidDependencyWheel("wheelhouse must not contain two artifacts for one name")
        by_name[wheel.name] = filename
        by_filename[filename] = source if isinstance(source, bytes) else source.read_bytes()
    return by_filename, by_name


# --------------------------------------------------------------------------------------------
# Offline dependency-closure validation
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MarkerEnvironment:
    """One concrete environment a marker is evaluated against."""

    python_version: str
    sys_platform: str

    def as_marker_environment(self) -> dict[str, str]:
        is_windows = self.sys_platform == "win32"
        return {
            "python_version": self.python_version,
            "python_full_version": f"{self.python_version}.0",
            "os_name": "nt" if is_windows else "posix",
            "sys_platform": self.sys_platform,
            "platform_system": "Windows" if is_windows else "Linux",
            "platform_machine": "AMD64" if is_windows else "x86_64",
            "implementation_name": "cpython",
            "platform_python_implementation": "CPython",
            "extra": "",
        }


# V1 is pure-Python and must work on both supported platforms, so a dependency that is required on
# *either* platform must be present. Evaluating both is what makes the self-containment claim honest
# for a package authored on one OS and installed on the other.
TARGET_ENVIRONMENTS = (
    MarkerEnvironment(python_version=_TARGET_PYTHON_VERSION, sys_platform="linux"),
    MarkerEnvironment(python_version=_TARGET_PYTHON_VERSION, sys_platform="win32"),
)


def _requirement_marker_applies(requirement: Requirement, environment: MarkerEnvironment) -> bool:
    marker = requirement.marker
    if marker is None:
        return True
    return marker.evaluate(environment.as_marker_environment())


def validate_dependency_closure(
    wheels: Mapping[str, WheelSource],
    *,
    extra_requires: Iterable[str] = (),
) -> None:
    """Prove offline that the wheelhouse satisfies what the package and its wheels require.

    Each requirement is checked against both frozen Python 3.12 target platforms. A dependency that
    applies unconditionally, or on either platform, must be
    present -- which is why the check is per-environment and not per-platform-agnostic. Requirements
    that apply only under an `extra` are skipped: V1 ships no extras, and the frozen policy is that
    an optional dependency may be omitted.

    This is a *presence* proof over already-resolved artifacts. There is no resolver here, no index
    client, and no network call anywhere in this module: a requirement whose distribution is absent
    is an error, never something to go and fetch.
    """
    build_wheelhouse_index(wheels)  # rejects duplicate distribution identities
    requirement_budget = RequirementBudget()
    metadata_by_name = {
        metadata.name: metadata
        for metadata in (
            inspect_wheel(
                wheels[filename], filename=filename, requirement_budget=requirement_budget
            )
            for filename in sorted(wheels, key=lambda item: item.encode("utf-8"))
        )
    }

    for wheel_bytes_name in sorted(wheels, key=lambda item: item.encode("utf-8")):
        metadata = inspect_wheel(wheels[wheel_bytes_name], filename=wheel_bytes_name)
        _validate_requirements(metadata.requires_dist, metadata_by_name, source=metadata.name)

    _validate_requirements(
        extra_requires, metadata_by_name, source="agent wheel", budget=requirement_budget
    )


def _validate_requirements(
    requirements: Iterable[str],
    available: Mapping[str, WheelMetadata],
    *,
    source: str,
    budget: RequirementBudget | None = None,
) -> None:
    # Every raw occurrence is still retained in WheelMetadata and visited here.
    # Only successfully validated identical short values are memoized within this
    # immutable closure check. Invalid declarations still raise at their original
    # position. Cache bounds affect optimization only, never accepted metadata.
    validated: set[str] = set()
    marker_tails: dict[str, bool] = {}
    for raw_requirement in requirements:
        if budget is not None:
            budget.consume(raw_requirement)
        if type(raw_requirement) is str and raw_requirement in validated:
            continue
        if type(raw_requirement) is str and len(raw_requirement) <= 1024:
            matched = _BARE_MARKED_REQUIREMENT.fullmatch(raw_requirement)
            if matched:
                tail = matched.group(1)
                if tail not in marker_tails and len(marker_tails) < 128:
                    try:
                        template = Requirement("nervosmarker" + tail)
                    except InvalidRequirement:
                        # The complete original parser below retains error evidence.
                        pass
                    else:
                        marker_tails[tail] = any(
                            _requirement_marker_applies(template, environment)
                            for environment in TARGET_ENVIRONMENTS
                        )
                if marker_tails.get(tail) is False:
                    # Name is guaranteed valid by the conservative lexical subset;
                    # the standard parser validated every byte after it. Neither
                    # marker parsing nor evaluation depends on the distribution name.
                    # No occurrence is removed from the returned wheel metadata.
                    continue
        requirement = _parse_requirement(raw_requirement)
        applicable = any(
            _requirement_marker_applies(requirement, environment)
            for environment in TARGET_ENVIRONMENTS
        )
        if applicable:
            _require_satisfied(requirement, available, source=source)
        if type(raw_requirement) is str and len(validated) < 128 and len(raw_requirement) <= 1024:
            validated.add(raw_requirement)


def _require_satisfied(
    requirement: Requirement,
    available: Mapping[str, WheelMetadata],
    *,
    source: str,
) -> None:
    if requirement.url is not None:
        raise DependencyClosureError(
            f"{source} declares a direct-reference dependency; V1 permits offline wheelhouse "
            "artifacts only"
        )
    name = normalized_distribution_name(requirement.name)
    dependency = available.get(name)
    if dependency is None:
        raise DependencyClosureError(
            f"{source} requires dependency {name!r}, which is not in the wheelhouse"
        )
    if requirement.specifier and Version(dependency.version) not in requirement.specifier:
        raise DependencyClosureError(
            f"{source} requires {name!r}{requirement.specifier}, but the wheelhouse pins "
            f"{dependency.version}"
        )


def _parse_requirement(value: str) -> Requirement:
    try:
        return Requirement(value)
    except InvalidRequirement as error:
        raise DependencyClosureError("wheel declares an unparseable requirement") from error


def agent_requires_dist(raw: WheelSource) -> tuple[str, ...]:
    """The agent wheel's declared requirements, read from `METADATA` bytes."""
    archive = BoundedArchiveReader(raw, profile=ArchiveValidationProfile.WHEEL)
    metadata_paths = _split_wheel_member(archive.paths(), _METADATA_MEMBER_SUFFIX)
    if len(metadata_paths) != 1:
        raise InvalidAgentWheel("agent wheel must contain exactly one METADATA")
    return tuple(
        value
        for _, value in _bounded_wheel_headers(
            archive.stream(metadata_paths[0]), frozenset({"requires-dist"}), RequirementBudget()
        )
    )
