"""Stage G package identity, SemVer, and manifest domain values.

These are the frozen G1 value contracts: a package id that reuses the repository's exact Agent key
grammar, strict SemVer 2.0.0 package versions, the manifest protocol version (a distinct concept
from the package release version), the inclusive NervOS compatibility range, and declarative
manifest metadata. Nothing here is a durable record and nothing here grants authority: the
declarations are inert values a later milestone may read.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import total_ordering

from nervos_core.domain.agents import AGENT_KEY_PATTERN, MAX_AGENT_KEY_LENGTH, AgentDefinitionId
from nervos_core.domain.runs import RunLimits
from nervos_core.domain.triggers import TriggerKind

# Strict SemVer 2.0.0: MAJOR.MINOR.PATCH[-prerelease][+build], no leading zeroes, no empty parts.
SEMVER_PATTERN = re.compile(
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?\Z"
)
CAPABILITY_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")
EXTENSION_KEY_PATTERN = re.compile(r"x-nervos-[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_PYTHON_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class InvalidPackageEntrypoint(ValueError):
    """Raised when `runtime.entrypoint` is not `module.path:Symbol` syntax."""


def validate_entrypoint(value: str) -> str:
    """Validate `module.path:Symbol` syntax without importing or loading anything.

    Only the shape is checked. The module is never imported, the wheel is never inspected, and no
    filesystem path or relative import is representable: exactly one `:`, an absolute dotted module
    of Python identifiers, and one Python identifier symbol.
    """
    if value.count(":") != 1:
        raise InvalidPackageEntrypoint("runtime.entrypoint must be module.path:Symbol")
    module, _, symbol = value.partition(":")
    if not module or not symbol or "/" in value or "\\" in value or ".." in module:
        raise InvalidPackageEntrypoint("invalid runtime.entrypoint")
    if any(_PYTHON_IDENTIFIER.fullmatch(part) is None for part in module.split(".")):
        raise InvalidPackageEntrypoint("invalid runtime.entrypoint module")
    if _PYTHON_IDENTIFIER.fullmatch(symbol) is None:
        raise InvalidPackageEntrypoint("invalid runtime.entrypoint symbol")
    return value


MANIFEST_MAX_BYTES = 65_536
MAX_ASSET_PATH_BYTES = 1024
MAX_ASSET_COUNT = 256
SUPPORTED_MANIFEST_VERSION = "1"
SUPPORTED_RUNTIME_LANGUAGE = "python"
# Frozen: V1 is exactly Python 3.12. The declaration is the exact text `>=3.12,<4`, not a range
# the parser resolves; anything else is a different runtime and is refused by construction.
SUPPORTED_RUNTIME_PYTHON = ">=3.12,<4"
# The core-owned current NervOS version used for compatibility validation. Kept in sync with the
# distribution metadata by a unit test rather than read from it at import time: a domain value must
# not depend on installed packaging metadata.
NERVOS_CORE_COMPATIBILITY_VERSION = "0.1.0"

_TEXT_MAX_CODE_POINTS = 256
_TEXT_MAX_BYTES = 1024
_DESCRIPTION_MAX_CODE_POINTS = 4000
_DESCRIPTION_MAX_BYTES = 16_000
_CONFIG_SCHEMA_REF = "config.schema.json"


class InvalidPackageIdentity(ValueError):
    """Raised when a package id is invalid or reserved."""


class InvalidPackageVersion(ValueError):
    """Raised when a package version is not strict SemVer 2.0.0."""


class InvalidPackageManifest(ValueError):
    """Raised when a parsed package manifest violates the V1 contract."""


def validate_package_id(value: str, *, allow_reserved: bool = False) -> str:
    """Validate a V1 package id without normalization or rewriting.

    The grammar is the repository's `AGENT_KEY_PATTERN` verbatim; the `nervos.*` namespace is
    reserved for node/built-in/system software, so a third-party package may never occupy it.
    """
    if len(value) > MAX_AGENT_KEY_LENGTH or AGENT_KEY_PATTERN.fullmatch(value) is None:
        raise InvalidPackageIdentity("invalid package_id")
    if not allow_reserved and value.startswith("nervos."):
        raise InvalidPackageIdentity("package_id uses reserved nervos namespace")
    return value


def _validate_text(
    value: object,
    *,
    field: str,
    max_code_points: int = _TEXT_MAX_CODE_POINTS,
    max_bytes: int = _TEXT_MAX_BYTES,
) -> str:
    if not isinstance(value, str):
        raise InvalidPackageManifest(f"{field} must be text")
    if (
        not value
        or value != value.strip()
        or len(value) > max_code_points
        or len(value.encode("utf-8")) > max_bytes
        or any(ord(character) < 32 for character in value)
    ):
        raise InvalidPackageManifest(f"invalid {field}")
    return value


def _validate_optional_text(
    value: object,
    *,
    field: str,
    max_code_points: int = _DESCRIPTION_MAX_CODE_POINTS,
    max_bytes: int = _DESCRIPTION_MAX_BYTES,
) -> str | None:
    if value is None:
        return None
    return _validate_text(value, field=field, max_code_points=max_code_points, max_bytes=max_bytes)


@total_ordering
@dataclass(frozen=True, slots=True)
class PackageVersion:
    """Strict SemVer 2.0.0 package release version, bounded to fit an exact identity."""

    value: str

    def __post_init__(self) -> None:
        if len(self.value) > 64:
            raise InvalidPackageVersion("package_version is too long")
        match = SEMVER_PATTERN.fullmatch(self.value)
        if match is None:
            raise InvalidPackageVersion("package_version must be strict SemVer 2.0.0")
        prerelease = match.group(4)
        if prerelease is not None:
            for identifier in prerelease.split("."):
                if identifier.isdigit() and identifier.startswith("0") and identifier != "0":
                    raise InvalidPackageVersion(
                        "numeric prerelease identifiers must not have leading zeroes"
                    )

    def _match(self) -> re.Match[str]:
        match = SEMVER_PATTERN.fullmatch(self.value)
        assert match is not None  # construction proved it
        return match

    @property
    def major(self) -> int:
        return int(self._match().group(1))

    @property
    def minor(self) -> int:
        return int(self._match().group(2))

    @property
    def patch(self) -> int:
        return int(self._match().group(3))

    @property
    def prerelease(self) -> tuple[str, ...]:
        value = self._match().group(4)
        return () if value is None else tuple(value.split("."))

    @property
    def build(self) -> str | None:
        return self._match().group(5)

    def __str__(self) -> str:
        return self.value

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, PackageVersion):
            return NotImplemented
        release = (self.major, self.minor, self.patch)
        other_release = (other.major, other.minor, other.patch)
        if release != other_release:
            return release < other_release
        return _prerelease_precedes(self.prerelease, other.prerelease)


def _prerelease_precedes(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    """SemVer precedence: a release outranks any prerelease; numeric ids compare numerically."""
    if not left and not right:
        return False
    if not left:
        return False
    if not right:
        return True
    for left_part, right_part in zip(left, right, strict=False):
        if left_part == right_part:
            continue
        left_numeric = left_part.isdigit()
        right_numeric = right_part.isdigit()
        if left_numeric and right_numeric:
            return int(left_part) < int(right_part)
        if left_numeric != right_numeric:
            return left_numeric
        return left_part < right_part
    return len(left) < len(right)


@dataclass(frozen=True, slots=True)
class ManifestVersion:
    """The manifest protocol/schema version, distinct from the package release version."""

    value: str

    def __post_init__(self) -> None:
        if self.value != SUPPORTED_MANIFEST_VERSION:
            raise InvalidPackageManifest("manifest_version must be 1")


@dataclass(frozen=True, slots=True)
class PackageRef:
    """An exact package id/version reference."""

    package_id: str
    package_version: PackageVersion

    def __post_init__(self) -> None:
        validate_package_id(self.package_id)


@dataclass(frozen=True, slots=True)
class PackageIdentity(PackageRef):
    """The exact V1 package identity, which couples package and definition identity."""

    def as_agent_definition_id(self) -> AgentDefinitionId:
        return AgentDefinitionId(self.package_id, self.package_version.value)


@dataclass(frozen=True, slots=True)
class PackageRuntime:
    """The V1 runtime declaration."""

    language: str
    python: str
    entrypoint: str

    def __post_init__(self) -> None:
        if self.language != SUPPORTED_RUNTIME_LANGUAGE:
            raise InvalidPackageManifest("runtime.language must be python")
        if self.python != SUPPORTED_RUNTIME_PYTHON:
            raise InvalidPackageManifest(f"runtime.python must be {SUPPORTED_RUNTIME_PYTHON}")
        validate_entrypoint(self.entrypoint)


@dataclass(frozen=True, slots=True)
class PackageCompatibility:
    """The inclusive NervOS compatibility range declared by a manifest.

    Both bounds are required and inclusive, `min_version <= max_version` must hold, and the current
    running NervOS version must fall inside the range. There is no open-ended or inferred bound.
    """

    min_version: PackageVersion
    max_version: PackageVersion

    def __post_init__(self) -> None:
        if self.max_version < self.min_version:
            raise InvalidPackageManifest("nervos.min_version must be <= nervos.max_version")
        current = PackageVersion(NERVOS_CORE_COMPATIBILITY_VERSION)
        if current < self.min_version or self.max_version < current:
            raise InvalidPackageManifest("manifest is not compatible with this NervOS version")


@dataclass(frozen=True, slots=True)
class PackageConfiguration:
    """Reference to the package configuration schema inside the archive."""

    schema: str

    def __post_init__(self) -> None:
        if self.schema != _CONFIG_SCHEMA_REF:
            raise InvalidPackageManifest(f"configuration.schema must be {_CONFIG_SCHEMA_REF}")


@dataclass(frozen=True, slots=True)
class PackageModelRequirements:
    """Declarative model capability requirements; never a provider binding."""

    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(set(self.capabilities)) != len(self.capabilities):
            raise InvalidPackageManifest("model capabilities must be unique")
        for capability in self.capabilities:
            if CAPABILITY_PATTERN.fullmatch(capability) is None:
                raise InvalidPackageManifest("invalid model capability")


@dataclass(frozen=True, slots=True)
class PackageToolRequirements:
    """Portable built-in tool requirements, expressed by the built-in upstream name.

    `required` affects readiness only and `optional` may be absent; neither grants access. A
    database primary key, an MCP connection id, or a node-local id is never a valid entry.
    """

    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        combined = (*self.required, *self.optional)
        if len(set(combined)) != len(combined):
            raise InvalidPackageManifest("tool requirements must be unique")
        if any(not value for value in combined):
            raise InvalidPackageManifest("tool requirements must be non-empty")


@dataclass(frozen=True, slots=True)
class PackageTriggerDeclarations:
    """Declarative supported trigger kinds; never activation authority.

    A kind must be one of the five Stage-E `TriggerKind` members. Installing a package never
    creates, enables, or activates a trigger, so this is inert UX metadata.
    """

    supported: tuple[TriggerKind, ...] = ()

    def __post_init__(self) -> None:
        if len(set(self.supported)) != len(self.supported):
            raise InvalidPackageManifest("trigger declarations must be unique")


@dataclass(frozen=True, slots=True)
class PackageMemoryDeclarations:
    """Declarative memory metadata; never read/write authority.

    A default of `reads=False, writes=False` is the honest one: nothing about installing a package
    widens Stage-F memory authority, and there are zero automatic memory writes.
    """

    reads: bool = False
    writes: bool = False

    def __post_init__(self) -> None:
        if type(self.reads) is not bool or type(self.writes) is not bool:
            raise InvalidPackageManifest("memory declarations must be booleans")


@dataclass(frozen=True, slots=True)
class PackageAssetDeclarations:
    """Declared package-relative asset paths; inert metadata with no filesystem authority.

    The frozen contract fixes only that a manifest *may* declare assets and that the archive holds
    them under `assets/`. G1 records the declared paths and checks each is a safe relative path;
    G2 is what resolves them against a real archive. A declaration here opens nothing and grants
    nothing.
    """

    paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(set(self.paths)) != len(self.paths):
            raise InvalidPackageManifest("asset paths must be unique")
        if len(self.paths) > MAX_ASSET_COUNT:
            raise InvalidPackageManifest("too many declared assets")
        for path in self.paths:
            _validate_asset_path(path)


def _validate_asset_path(value: str) -> None:
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or ":" in value
        or len(value.encode("utf-8")) > MAX_ASSET_PATH_BYTES
        or any(ord(character) < 32 for character in value)
        or any(segment in {"", ".", ".."} for segment in value.split("/"))
    ):
        raise InvalidPackageManifest("invalid asset path")


@dataclass(frozen=True, slots=True)
class PackageResources:
    """Bounded resource hints that project deterministically onto RunLimits in G1."""

    limits: RunLimits = field(default_factory=RunLimits)


@dataclass(frozen=True, slots=True)
class PackageManifest:
    """The canonical internal representation of a parsed V1 `manifest.yaml`."""

    manifest_version: ManifestVersion
    identity: PackageIdentity
    package_name: str
    display_name: str
    publisher: str
    description: str | None
    runtime: PackageRuntime
    nervos: PackageCompatibility
    configuration: PackageConfiguration
    models: PackageModelRequirements
    tools: PackageToolRequirements
    triggers: PackageTriggerDeclarations
    memory: PackageMemoryDeclarations
    assets: PackageAssetDeclarations
    resources: PackageResources
    extensions: Mapping[str, object]

    def __post_init__(self) -> None:
        _validate_text(self.package_name, field="package_name")
        _validate_text(self.display_name, field="display_name")
        _validate_text(self.publisher, field="publisher")
        _validate_optional_text(self.description, field="description")

    @property
    def package_id(self) -> str:
        return self.identity.package_id

    @property
    def package_version(self) -> str:
        return self.identity.package_version.value
