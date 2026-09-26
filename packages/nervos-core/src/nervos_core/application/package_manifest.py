"""Strict V1 package manifest parsing and the deterministic AgentDefinition projection.

The V1 manifest is exactly `manifest.yaml`, and it is parsed as data only: a strict fail-closed YAML
data subset with JSON-style scalar semantics, no aliases/anchors/merge keys, no duplicate keys, no
custom tags, no multi-document input, and unknown fields rejected unless they are a top-level
lowercase `x-nervos-*` extension. Parsing never executes package code, and the byte API is bounded
before YAML ever sees the input.

The parsed :class:`PackageManifest` is a declaration, never authority: nothing here grants a
Stage-D tool, widens Stage-F memory, or creates/enables a Stage-E trigger. Tool requirements are
resolved against the portable built-in identities (the `upstream_name` of a Stage-D built-in spec)
so an unknown or node-local identifier fails closed at parse time.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import cast

import yaml

from nervos_core.application.builtin_tools import builtin_tool_specs
from nervos_core.application.clock import Clock
from nervos_core.domain.agents import AgentDefinition
from nervos_core.domain.packages import (
    EXTENSION_KEY_PATTERN,
    MANIFEST_MAX_BYTES,
    InvalidPackageManifest,
    ManifestVersion,
    PackageAssetDeclarations,
    PackageCompatibility,
    PackageConfiguration,
    PackageIdentity,
    PackageManifest,
    PackageMemoryDeclarations,
    PackageModelRequirements,
    PackageResources,
    PackageRuntime,
    PackageToolRequirements,
    PackageTriggerDeclarations,
    PackageVersion,
    validate_package_id,
)
from nervos_core.domain.runs import RunLimits
from nervos_core.domain.triggers import TriggerKind

_BOOL_TRUE = "tag:yaml.org,2002:bool"
_NON_JSON_SCALAR_TAGS = frozenset({_BOOL_TRUE, "tag:yaml.org,2002:timestamp"})
_JSON_BOOL = re.compile(r"^(?:true|false)$")

_BASE_KEYS = frozenset(
    {
        "manifest_version",
        "package_id",
        "package_name",
        "package_version",
        "publisher",
        "display_name",
        "description",
        "runtime",
        "nervos",
        "configuration",
        "models",
        "tools",
        "triggers",
        "memory",
        "assets",
        "resources",
    }
)
_REQUIRED_KEYS = frozenset(
    {
        "manifest_version",
        "package_id",
        "package_name",
        "package_version",
        "publisher",
        "display_name",
        "runtime",
        "nervos",
        "configuration",
    }
)
_RUNTIME_KEYS = frozenset({"language", "python", "entrypoint"})
_NERVOS_KEYS = frozenset({"min_version", "max_version"})
_CONFIGURATION_KEYS = frozenset({"schema"})
_MODELS_KEYS = frozenset({"capabilities"})
_TOOLS_KEYS = frozenset({"required", "optional"})
_TRIGGERS_KEYS = frozenset({"supported"})
_MEMORY_KEYS = frozenset({"reads", "writes"})
_RESOURCES_KEYS = frozenset({"limits"})
_LIMIT_KEYS = frozenset(
    {
        "input_max_bytes",
        "input_max_code_points",
        "output_max_bytes",
        "output_max_code_points",
        "provider_timeout_ms",
        "max_output_tokens",
        "max_model_calls",
        "max_tool_calls",
        "tool_timeout_ms",
        "tool_result_max_bytes",
    }
)


class _StrictLoader(yaml.SafeLoader):
    """Safe loader with JSON-style scalars and no alias/anchor resolution."""

    def fetch_alias(self) -> None:  # type: ignore[override]
        raise InvalidPackageManifest("manifest aliases and anchors are not allowed")


# YAML 1.1 resolves `yes/no/on/off` to booleans and `2026-01-01` to a date. Neither is a JSON-style
# scalar, so this loader drops those two implicit resolvers and installs JSON's exact `true/false`.
_StrictLoader.yaml_implicit_resolvers = {  # type: ignore[assignment]
    key: [(tag, regex) for tag, regex in entries if tag not in _NON_JSON_SCALAR_TAGS]
    for key, entries in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_StrictLoader.add_implicit_resolver(  # pyright: ignore[reportUnknownMemberType]
    _BOOL_TRUE, _JSON_BOOL, list("tf")
)


def _construct_mapping(
    loader: yaml.Loader, node: yaml.Node, deep: bool = False
) -> dict[object, object]:
    if not isinstance(node, yaml.MappingNode):
        raise InvalidPackageManifest("manifest mapping expected")
    construct = cast(
        Callable[[yaml.Node, bool], object],
        loader.construct_object,  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
    )
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            raise InvalidPackageManifest("manifest merge keys are not allowed")
        key = construct(key_node, deep)
        if key in result:
            raise InvalidPackageManifest("manifest duplicate keys are not allowed")
        result[key] = construct(value_node, deep)
    return result


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def parse_package_manifest(raw: bytes, *, clock: Clock | None = None) -> PackageManifest:
    """Parse strict UTF-8 manifest bytes, bounded before any YAML parsing happens.

    The 64 KiB pre-parse limit and strict UTF-8 decoding come first so an oversized or malformed
    archive member is refused without building a parser tree from it.
    """
    if len(raw) > MANIFEST_MAX_BYTES:
        raise InvalidPackageManifest("manifest exceeds the 64 KiB limit")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise InvalidPackageManifest("manifest must be strict UTF-8") from error

    try:
        documents = list(yaml.load_all(text, Loader=_StrictLoader))
    except InvalidPackageManifest:
        raise
    except yaml.YAMLError as error:
        raise InvalidPackageManifest("invalid manifest yaml") from error
    if len(documents) != 1 or not isinstance(documents[0], Mapping):
        raise InvalidPackageManifest("manifest must contain exactly one mapping document")

    root = _as_text_mapping(cast(Mapping[object, object], documents[0]), "manifest")
    manifest = parse_package_manifest_mapping(root)
    validate_manifest_tool_ids(manifest, clock=clock)
    return manifest


def parse_package_manifest_text(text: str, *, clock: Clock | None = None) -> PackageManifest:
    """Convenience wrapper around :func:`parse_package_manifest` for already-decoded text."""
    return parse_package_manifest(text.encode("utf-8"), clock=clock)


def parse_package_manifest_mapping(data: Mapping[str, object]) -> PackageManifest:
    """Validate and canonicalize a decoded V1 manifest mapping."""
    extensions = _collect_extensions(data)
    _require_keys(data, _REQUIRED_KEYS, "manifest")
    package_id = _required_text(data, "package_id")
    validate_package_id(package_id)

    return PackageManifest(
        manifest_version=ManifestVersion(_required_text(data, "manifest_version")),
        identity=PackageIdentity(
            package_id, PackageVersion(_required_text(data, "package_version"))
        ),
        package_name=_required_text(data, "package_name"),
        display_name=_required_text(data, "display_name"),
        publisher=_required_text(data, "publisher"),
        description=_optional_text(data, "description"),
        runtime=_parse_runtime(_required_mapping(data, "runtime")),
        nervos=_parse_nervos(_required_mapping(data, "nervos")),
        configuration=_parse_configuration(_required_mapping(data, "configuration")),
        models=_parse_models(_optional_mapping(data, "models")),
        tools=_parse_tools(_optional_mapping(data, "tools")),
        triggers=_parse_triggers(_optional_mapping(data, "triggers")),
        memory=_parse_memory(_optional_mapping(data, "memory")),
        assets=PackageAssetDeclarations(_text_tuple(data.get("assets"), "assets")),
        resources=_parse_resources(_optional_mapping(data, "resources")),
        extensions=MappingProxyType(extensions),
    )


def project_agent_definition(manifest: PackageManifest) -> AgentDefinition:
    """Project a manifest into the exact AgentDefinition G1 resolution exposes.

    Deterministic by construction: the identity is the frozen package/definition coupling, the
    display name is the declared one, and the limits come from the manifest resource hints or the
    documented fixed V1 RunLimits defaults when none were declared.
    """
    return AgentDefinition(
        identity=manifest.identity.as_agent_definition_id(),
        display_name=manifest.display_name,
        limits=manifest.resources.limits,
    )


def builtin_tool_ids(*, clock: Clock | None = None) -> tuple[str, ...]:
    """The portable built-in tool identities, with the clock injected rather than read globally."""
    return tuple(spec.upstream_name for spec in builtin_tool_specs(clock=clock or _fixed_clock))


def validate_manifest_tool_ids(manifest: PackageManifest, *, clock: Clock | None = None) -> None:
    """Fail closed when a declared tool is not a portable built-in identity."""
    available = frozenset(builtin_tool_ids(clock=clock))
    for tool_id in (*manifest.tools.required, *manifest.tools.optional):
        if tool_id not in available:
            raise InvalidPackageManifest(f"unknown portable built-in tool id: {tool_id}")


def _fixed_clock() -> datetime:
    """A fixed instant so the default tool-identity query has no global-time dependency."""
    return datetime(2026, 1, 1, tzinfo=UTC)


def _collect_extensions(data: Mapping[str, object]) -> dict[str, object]:
    extensions: dict[str, object] = {}
    for key, value in data.items():
        if key in _BASE_KEYS:
            continue
        if EXTENSION_KEY_PATTERN.fullmatch(key) is None:
            raise InvalidPackageManifest(f"unknown manifest field: {key}")
        extensions[key] = _freeze_json(value, key)
    return extensions


def _parse_runtime(data: Mapping[str, object]) -> PackageRuntime:
    _exact_keys(data, _RUNTIME_KEYS, "runtime")
    return PackageRuntime(
        language=_required_text(data, "language"),
        python=_required_text(data, "python"),
        entrypoint=_required_text(data, "entrypoint"),
    )


def _parse_nervos(data: Mapping[str, object]) -> PackageCompatibility:
    _exact_keys(data, _NERVOS_KEYS, "nervos")
    return PackageCompatibility(
        min_version=PackageVersion(_required_text(data, "min_version")),
        max_version=PackageVersion(_required_text(data, "max_version")),
    )


def _parse_configuration(data: Mapping[str, object]) -> PackageConfiguration:
    _exact_keys(data, _CONFIGURATION_KEYS, "configuration")
    return PackageConfiguration(schema=_required_text(data, "schema"))


def _parse_models(data: Mapping[str, object] | None) -> PackageModelRequirements:
    if data is None:
        return PackageModelRequirements()
    _reject_unknown_nested(data, _MODELS_KEYS, "models")
    return PackageModelRequirements(_text_tuple(data.get("capabilities"), "models.capabilities"))


def _parse_tools(data: Mapping[str, object] | None) -> PackageToolRequirements:
    if data is None:
        return PackageToolRequirements()
    _reject_unknown_nested(data, _TOOLS_KEYS, "tools")
    return PackageToolRequirements(
        required=_text_tuple(data.get("required"), "tools.required"),
        optional=_text_tuple(data.get("optional"), "tools.optional"),
    )


def _parse_triggers(data: Mapping[str, object] | None) -> PackageTriggerDeclarations:
    if data is None:
        return PackageTriggerDeclarations()
    _reject_unknown_nested(data, _TRIGGERS_KEYS, "triggers")
    kinds: list[TriggerKind] = []
    for value in _text_tuple(data.get("supported"), "triggers.supported"):
        try:
            kinds.append(TriggerKind(value))
        except ValueError as error:
            raise InvalidPackageManifest(f"invalid trigger kind: {value}") from error
    return PackageTriggerDeclarations(tuple(kinds))


def _parse_memory(data: Mapping[str, object] | None) -> PackageMemoryDeclarations:
    if data is None:
        return PackageMemoryDeclarations()
    _reject_unknown_nested(data, _MEMORY_KEYS, "memory")
    return PackageMemoryDeclarations(
        reads=_boolean(data.get("reads", False), "memory.reads"),
        writes=_boolean(data.get("writes", False), "memory.writes"),
    )


def _parse_resources(data: Mapping[str, object] | None) -> PackageResources:
    if data is None:
        return PackageResources()
    _reject_unknown_nested(data, _RESOURCES_KEYS, "resources")
    limits_data = data.get("limits")
    if limits_data is None:
        return PackageResources()
    if not isinstance(limits_data, Mapping):
        raise InvalidPackageManifest("resources.limits must be a mapping")
    declared = _as_text_mapping(cast(Mapping[object, object], limits_data), "resources.limits")
    _reject_unknown_nested(declared, _LIMIT_KEYS, "resources.limits")

    defaults = RunLimits()
    values: dict[str, int] = {name: getattr(defaults, name) for name in _LIMIT_KEYS}
    for name, value in declared.items():
        if not isinstance(value, int) or isinstance(value, bool):
            raise InvalidPackageManifest(f"resources.limits.{name} must be an integer")
        values[name] = value
    try:
        return PackageResources(limits=RunLimits(**values))
    except ValueError as error:
        raise InvalidPackageManifest("invalid resources.limits") from error


def _exact_keys(data: Mapping[str, object], keys: frozenset[str], path: str) -> None:
    _reject_unknown_nested(data, keys, path)
    _require_keys(data, keys, path)


def _reject_unknown_nested(data: Mapping[str, object], allowed: frozenset[str], path: str) -> None:
    unknown = data.keys() - allowed
    if unknown:
        raise InvalidPackageManifest(f"unknown {path} field: {sorted(unknown)[0]}")


def _require_keys(data: Mapping[str, object], keys: frozenset[str], path: str) -> None:
    missing = keys - data.keys()
    if missing:
        raise InvalidPackageManifest(f"missing required {path} field: {sorted(missing)[0]}")


def _as_text_mapping(data: Mapping[object, object], path: str) -> Mapping[str, object]:
    result: dict[str, object] = {}
    for key, value in data.items():
        if not isinstance(key, str):
            raise InvalidPackageManifest(f"{path} keys must be text")
        result[key] = value
    return result


def _required_mapping(data: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = data.get(key)
    if not isinstance(value, Mapping):
        raise InvalidPackageManifest(f"{key} must be a mapping")
    return _as_text_mapping(cast(Mapping[object, object], value), key)


def _optional_mapping(data: Mapping[str, object], key: str) -> Mapping[str, object] | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise InvalidPackageManifest(f"{key} must be a mapping")
    return _as_text_mapping(cast(Mapping[object, object], value), key)


def _required_text(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise InvalidPackageManifest(f"{key} must be text")
    return value


def _optional_text(data: Mapping[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidPackageManifest(f"{key} must be text")
    return value


def _text_tuple(value: object, path: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise InvalidPackageManifest(f"{path} must be a list")
    entries = cast(list[object], value)
    if any(not isinstance(entry, str) or not entry for entry in entries):
        raise InvalidPackageManifest(f"{path} entries must be non-empty text")
    return tuple(cast(list[str], entries))


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidPackageManifest(f"{path} must be a boolean")
    return value


def _freeze_json(value: object, path: str) -> object:
    """Canonicalize an extension value to an immutable JSON-compatible representation."""
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, list):
        return tuple(_freeze_json(item, path) for item in cast(list[object], value))
    if isinstance(value, Mapping):
        mapping = _as_text_mapping(cast(Mapping[object, object], value), path)
        return MappingProxyType({key: _freeze_json(item, path) for key, item in mapping.items()})
    raise InvalidPackageManifest(f"{path} must contain only JSON-compatible data")
