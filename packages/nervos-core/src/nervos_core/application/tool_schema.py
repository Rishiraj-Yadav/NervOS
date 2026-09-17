"""The canonical tool schema subset, and the two validators over it.

NervOS owns this validator rather than delegating to a general-purpose JSON Schema library, so that
the subset definition and its enforcement cannot drift and so that no code path can be induced to
resolve a reference.

Two distinct responsibilities live here and are deliberately kept apart:

* :func:`validate_canonical_schema` -- *is this schema part of NervOS's supported subset?*
* :func:`validate_instance` -- *do these values conform to an already-accepted canonical schema?*

Neither resolves ``$ref``, downloads anything, executes anything, coerces silently, drops unknown
fields silently, or weakens an unsupported construct. Both are **total**: an invalid document is a
returned verdict, never an exception.

Byte ceilings are **not** universal. :func:`validate_instance` applies a structural depth bound
always and a byte ceiling only when the caller supplies one, because Stage D freezes
``Run.tool_result_max_bytes`` as a Run-snapshotted, configurable limit that D4 must be able to
enforce exactly. Hardcoding a ceiling here would silently overrule a Run's own contract.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from nervos_core.domain.tools import (
    JSON_VALUE_MAX_DEPTH,
    JsonRejection,
    JsonValue,
    JsonValueReason,
    canonical_json_text,
    validate_json_value,
)

MAX_SCHEMA_DEPTH = 8
MAX_SCHEMA_NODES = 128
MAX_SCHEMA_BYTES = 65536
MAX_ENUM_ENTRIES = 32
MAX_DESCRIPTION_BYTES = 65536

# A schema node at schema-depth `d` sits at JSON depth about `2*d - 1` (the node object, then its
# `properties` or `items` value). This bound therefore sits strictly ABOVE what MAX_SCHEMA_DEPTH
# permits, so the schema walk -- not the JSON pre-flight -- decides the depth verdict, and a schema
# one level too deep is reported as a depth problem rather than as a malformed document. Its only
# job is to stop a pathological document from recursing without limit.
MAX_SCHEMA_JSON_DEPTH = 2 * MAX_SCHEMA_DEPTH + 4

OBJECT = "object"
ARRAY = "array"
STRING = "string"
NUMBER = "number"
INTEGER = "integer"
BOOLEAN = "boolean"

_OBJECT_KEYS = frozenset({"type", "properties", "required", "additionalProperties", "description"})
_SCALAR_KEYS = frozenset({"type", "enum", "description"})
_ARRAY_KEYS = frozenset({"type", "items", "description"})

_ALLOWED_KEYS: dict[str, frozenset[str]] = {
    OBJECT: _OBJECT_KEYS,
    ARRAY: _ARRAY_KEYS,
    STRING: _SCALAR_KEYS,
    NUMBER: _SCALAR_KEYS,
    INTEGER: _SCALAR_KEYS,
    BOOLEAN: _SCALAR_KEYS,
}

# The union, used only to name an offending keyword precisely when the node's own `type` is missing
# or invalid, so the reported reason is about the keyword rather than about the absent type.
_ANY_NODE_KEYS = _OBJECT_KEYS | _SCALAR_KEYS | _ARRAY_KEYS

_JSON_TYPE_NAMES = ((bool, "boolean"), (str, "string"), (int, "number"), (float, "number"))


class SchemaRejectionReason(StrEnum):
    """Typed reasons a schema is outside the canonical subset."""

    NOT_AN_OBJECT = "not_an_object"
    UNSUPPORTED_KEYWORD = "unsupported_keyword"
    ROOT_NOT_OBJECT = "root_not_object"
    PROPERTIES_MISSING = "properties_missing"
    PROPERTIES_NOT_OBJECT = "properties_not_object"
    REQUIRED_NOT_LIST = "required_not_list"
    REQUIRED_INCONSISTENT = "required_inconsistent"
    ADDITIONAL_PROPERTIES_NOT_FALSE = "additional_properties_not_false"
    ITEMS_MISSING = "items_missing"
    MALFORMED_TYPE = "malformed_type"
    MALFORMED_ENUM = "malformed_enum"
    MALFORMED_DESCRIPTION = "malformed_description"
    DEPTH_EXCEEDED = "depth_exceeded"
    NODE_LIMIT_EXCEEDED = "node_limit_exceeded"
    BYTES_EXCEEDED = "bytes_exceeded"


@dataclass(frozen=True, slots=True)
class SchemaRejection:
    """One typed reason a schema was rejected, with its location and any offending keyword."""

    reason: SchemaRejectionReason
    path: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class CanonicalSchema:
    """A schema proven to be inside the canonical subset."""

    root: dict[str, JsonValue]
    node_count: int


class InstanceRejectionReason(StrEnum):
    """Typed reasons a value does not conform to a canonical schema."""

    NOT_JSON = "not_json"
    TYPE_MISMATCH = "type_mismatch"
    UNKNOWN_PROPERTY = "unknown_property"
    MISSING_REQUIRED = "missing_required"
    ENUM_MISMATCH = "enum_mismatch"
    DEPTH_EXCEEDED = "depth_exceeded"
    BYTES_EXCEEDED = "bytes_exceeded"


@dataclass(frozen=True, slots=True)
class InstanceRejection:
    """One typed reason a value failed instance validation, with its location."""

    reason: InstanceRejectionReason
    path: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class InstanceLimits:
    """Context-supplied bounds for one instance-validation call.

    ``max_depth`` is structural safety and is always applied. ``max_bytes`` is a policy bound and is
    applied **only when the caller supplies one** -- D4 passes ``Run.tool_result_max_bytes``, and
    ``json_transform`` passes its own 64 KiB built-in limit. Nothing here hardcodes a ceiling.
    """

    max_depth: int = JSON_VALUE_MAX_DEPTH
    max_bytes: int | None = None


DEFAULT_INSTANCE_LIMITS = InstanceLimits()


def canonical_json_size(value: JsonValue) -> int:
    """Return the UTF-8 byte length of a value's canonical JSON form.

    Reusable and policy-free: this measures, it does not decide. Callers compare the result against
    whatever bound their own context carries.
    """
    return len(canonical_json_text(value).encode("utf-8"))


def validate_canonical_schema(schema: object) -> CanonicalSchema | SchemaRejection:
    """Prove a schema is inside the canonical subset, or say exactly why it is not."""
    rejection = validate_json_value(schema, max_depth=MAX_SCHEMA_JSON_DEPTH)
    if rejection is not None:
        if rejection.reason is JsonValueReason.DEPTH_EXCEEDED:
            return SchemaRejection(SchemaRejectionReason.DEPTH_EXCEEDED, rejection.path, None)
        return SchemaRejection(
            SchemaRejectionReason.NOT_AN_OBJECT, rejection.path, _describe_json_rejection(rejection)
        )
    if not isinstance(schema, dict):
        return SchemaRejection(SchemaRejectionReason.NOT_AN_OBJECT, "$")

    root = cast("dict[str, JsonValue]", schema)
    nodes = _NodeBudget()
    node_rejection = _validate_node(root, path="$", depth=1, nodes=nodes, is_root=True)
    if node_rejection is not None:
        return node_rejection

    size = len(canonical_json_text(root).encode("utf-8"))
    if size > MAX_SCHEMA_BYTES:
        return SchemaRejection(SchemaRejectionReason.BYTES_EXCEEDED, "$", str(size))
    return CanonicalSchema(root=root, node_count=nodes.count)


def validate_instance(
    schema: CanonicalSchema,
    value: object,
    *,
    limits: InstanceLimits = DEFAULT_INSTANCE_LIMITS,
) -> InstanceRejection | None:
    """Validate a value against an already-accepted canonical schema.

    Typing is strict and nothing is coerced or dropped: ``"5"`` is not an integer, ``5.0`` is not an
    integer, ``5`` *is* a number, and ``True`` is neither an integer nor a number.
    """
    json_rejection = validate_json_value(value, max_depth=limits.max_depth)
    if json_rejection is not None:
        if json_rejection.reason is JsonValueReason.DEPTH_EXCEEDED:
            return InstanceRejection(
                InstanceRejectionReason.DEPTH_EXCEEDED, json_rejection.path, None
            )
        return InstanceRejection(
            InstanceRejectionReason.NOT_JSON,
            json_rejection.path,
            _describe_json_rejection(json_rejection),
        )

    structural = _validate_against(schema.root, value, path="$")
    if structural is not None:
        return structural

    if limits.max_bytes is not None:
        size = canonical_json_size(cast("JsonValue", value))
        if size > limits.max_bytes:
            return InstanceRejection(InstanceRejectionReason.BYTES_EXCEEDED, "$", str(size))
    return None


def _validate_node(
    node: object,
    *,
    path: str,
    depth: int,
    nodes: _NodeBudget,
    is_root: bool,
) -> SchemaRejection | None:
    if depth > MAX_SCHEMA_DEPTH:
        return SchemaRejection(SchemaRejectionReason.DEPTH_EXCEEDED, path, str(depth))
    if not isinstance(node, dict):
        return SchemaRejection(SchemaRejectionReason.NOT_AN_OBJECT, path)
    mapping = cast("dict[str, object]", node)

    nodes.count += 1
    if nodes.count > MAX_SCHEMA_NODES:
        return SchemaRejection(SchemaRejectionReason.NODE_LIMIT_EXCEEDED, path, str(nodes.count))

    node_type = mapping.get("type")
    if not isinstance(node_type, str) or node_type not in _ALLOWED_KEYS:
        # A keyword that is invalid for *every* node type is named before the type is judged, so a
        # reference reports `$ref` rather than a misleading "malformed type". A `$ref` node usually
        # carries no `type` at all, and the user-visible reason must be the precise one.
        for keyword in mapping:
            if keyword not in _ANY_NODE_KEYS:
                return SchemaRejection(SchemaRejectionReason.UNSUPPORTED_KEYWORD, path, keyword)
        return SchemaRejection(SchemaRejectionReason.MALFORMED_TYPE, path, repr(node_type))
    if is_root and node_type != OBJECT:
        return SchemaRejection(SchemaRejectionReason.ROOT_NOT_OBJECT, path, node_type)

    allowed = _ALLOWED_KEYS[node_type]
    for keyword in mapping:
        if keyword not in allowed:
            return SchemaRejection(SchemaRejectionReason.UNSUPPORTED_KEYWORD, path, keyword)

    description = mapping.get("description")
    if description is not None and not _is_bounded_text(description, MAX_DESCRIPTION_BYTES):
        return SchemaRejection(SchemaRejectionReason.MALFORMED_DESCRIPTION, path)

    if node_type == OBJECT:
        return _validate_object_node(mapping, path=path, depth=depth, nodes=nodes)
    if node_type == ARRAY:
        return _validate_array_node(mapping, path=path, depth=depth, nodes=nodes)
    return _validate_scalar_node(mapping, node_type, path=path)


def _validate_object_node(
    node: dict[str, object], *, path: str, depth: int, nodes: _NodeBudget
) -> SchemaRejection | None:
    properties = node.get("properties")
    if properties is None:
        return SchemaRejection(SchemaRejectionReason.PROPERTIES_MISSING, path)
    if not isinstance(properties, dict):
        return SchemaRejection(SchemaRejectionReason.PROPERTIES_NOT_OBJECT, f"{path}.properties")
    property_map = cast("dict[str, object]", properties)

    for name, subschema in property_map.items():
        child = _validate_node(
            subschema,
            path=f"{path}.properties.{name}",
            depth=depth + 1,
            nodes=nodes,
            is_root=False,
        )
        if child is not None:
            return child

    additional = node.get("additionalProperties")
    if additional is not None and additional is not False:
        return SchemaRejection(
            SchemaRejectionReason.ADDITIONAL_PROPERTIES_NOT_FALSE, f"{path}.additionalProperties"
        )

    required = node.get("required")
    if required is not None:
        if not isinstance(required, list):
            return SchemaRejection(SchemaRejectionReason.REQUIRED_NOT_LIST, f"{path}.required")
        seen: set[str] = set()
        for entry in cast("list[object]", required):
            if not isinstance(entry, str) or entry not in property_map or entry in seen:
                return SchemaRejection(
                    SchemaRejectionReason.REQUIRED_INCONSISTENT, f"{path}.required", repr(entry)
                )
            seen.add(entry)
    return None


def _validate_array_node(
    node: dict[str, object], *, path: str, depth: int, nodes: _NodeBudget
) -> SchemaRejection | None:
    items = node.get("items")
    if items is None:
        return SchemaRejection(SchemaRejectionReason.ITEMS_MISSING, path)
    return _validate_node(items, path=f"{path}.items", depth=depth + 1, nodes=nodes, is_root=False)


def _validate_scalar_node(
    node: dict[str, object], node_type: str, *, path: str
) -> SchemaRejection | None:
    entries = node.get("enum")
    if entries is None:
        return None
    if not isinstance(entries, list):
        return SchemaRejection(SchemaRejectionReason.MALFORMED_ENUM, f"{path}.enum")
    entry_list = cast("list[object]", entries)
    if not 1 <= len(entry_list) <= MAX_ENUM_ENTRIES:
        return SchemaRejection(SchemaRejectionReason.MALFORMED_ENUM, f"{path}.enum")
    seen: set[tuple[str, str]] = set()
    for entry in entry_list:
        if not _matches_declared_type(node_type, entry):
            return SchemaRejection(
                SchemaRejectionReason.MALFORMED_ENUM, f"{path}.enum", repr(entry)
            )
        identity = (_json_type_name(entry), repr(entry))
        if identity in seen:
            return SchemaRejection(
                SchemaRejectionReason.MALFORMED_ENUM, f"{path}.enum", repr(entry)
            )
        seen.add(identity)
    return None


def _validate_against(
    schema: dict[str, JsonValue], value: object, *, path: str
) -> InstanceRejection | None:
    node_type = schema["type"]
    if node_type == OBJECT:
        return _validate_object_instance(schema, value, path=path)
    if node_type == ARRAY:
        return _validate_array_instance(schema, value, path=path)
    return _validate_scalar_instance(schema, str(node_type), value, path=path)


def _validate_object_instance(
    schema: dict[str, JsonValue], value: object, *, path: str
) -> InstanceRejection | None:
    if not isinstance(value, dict):
        return InstanceRejection(InstanceRejectionReason.TYPE_MISMATCH, path, OBJECT)
    keyed = cast("dict[str, object]", value)

    # Both are proven by schema validation, so the casts record a fact rather than assert a hope.
    properties = cast("dict[str, JsonValue]", cast(object, schema["properties"]))
    required = schema.get("required")

    if isinstance(required, list):
        for name in cast("list[object]", required):
            if isinstance(name, str) and name not in keyed:
                return InstanceRejection(InstanceRejectionReason.MISSING_REQUIRED, path, name)

    closed = schema.get("additionalProperties") is False
    for key, item in keyed.items():
        subschema = properties.get(key)
        if subschema is None:
            # An open object permits unknown keys as JSON values. They are kept, never dropped, and
            # they are not validated against a schema that does not exist. They were already checked
            # against the global JSON contract, including the depth bound.
            if closed:
                return InstanceRejection(InstanceRejectionReason.UNKNOWN_PROPERTY, path, key)
            continue
        if not isinstance(subschema, dict):
            return InstanceRejection(InstanceRejectionReason.NOT_JSON, f"{path}.{key}")
        child = _validate_against(
            cast("dict[str, JsonValue]", subschema), item, path=f"{path}.{key}"
        )
        if child is not None:
            return child
    return None


def _validate_array_instance(
    schema: dict[str, JsonValue], value: object, *, path: str
) -> InstanceRejection | None:
    if not isinstance(value, list):
        return InstanceRejection(InstanceRejectionReason.TYPE_MISMATCH, path, ARRAY)
    items = cast("dict[str, JsonValue]", cast(object, schema["items"]))
    for index, item in enumerate(cast("list[object]", value)):
        child = _validate_against(items, item, path=f"{path}[{index}]")
        if child is not None:
            return child
    return None


def _validate_scalar_instance(
    schema: dict[str, JsonValue], node_type: str, value: object, *, path: str
) -> InstanceRejection | None:
    if not _matches_declared_type(node_type, value):
        return InstanceRejection(InstanceRejectionReason.TYPE_MISMATCH, path, node_type)
    if isinstance(value, float) and not math.isfinite(value):
        return InstanceRejection(InstanceRejectionReason.TYPE_MISMATCH, path, node_type)

    entries = schema.get("enum")
    if isinstance(entries, list):
        for entry in cast("list[object]", entries):
            if _enum_matches(entry, value):
                return None
        return InstanceRejection(InstanceRejectionReason.ENUM_MISMATCH, path, repr(value))
    return None


def _matches_declared_type(node_type: str, value: object) -> bool:
    """Strict, coercion-free type membership.

    ``bool`` is excluded from ``integer`` and ``number`` explicitly, because ``bool`` is an ``int``
    subclass in Python and would otherwise satisfy both. ``integer`` additionally excludes ``float``
    while ``number`` accepts it -- so ``5`` is a number and ``5.0`` is not an integer.
    """
    if node_type == BOOLEAN:
        return isinstance(value, bool)
    if node_type == STRING:
        return isinstance(value, str)
    if node_type == INTEGER:
        return isinstance(value, int) and not isinstance(value, bool)
    if node_type == NUMBER:
        return isinstance(value, int | float) and not isinstance(value, bool)
    return False


def _enum_matches(entry: object, value: object) -> bool:
    """Type-aware enum equality.

    Compares the JSON type before the value, so ``True`` never satisfies ``enum: [1]`` and ``1``
    never satisfies ``enum: [true]`` -- Python's ``True == 1`` must not leak into a
    security-relevant validation decision.
    """
    if _json_type_name(entry) != _json_type_name(value):
        return False
    return bool(entry == value)


def _json_type_name(value: object) -> str:
    for python_type, name in _JSON_TYPE_NAMES:
        if isinstance(value, python_type):
            return name
    return "null"


def _is_bounded_text(value: object, max_bytes: int) -> bool:
    return isinstance(value, str) and len(value.encode("utf-8")) <= max_bytes


def _describe_json_rejection(rejection: JsonRejection) -> str:
    return str(rejection.reason)


class _NodeBudget:
    """Mutable counter for one schema walk."""

    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count = 0
