"""Package configuration schema and configuration validation for Stage G1.

This module deliberately exposes two separate APIs:

* schema APIs accept and validate the JSON Schema subset package authors may publish;
* config APIs accept user configuration, apply deterministic schema defaults, and validate the
  resulting effective configuration.

The supported schema language is a bounded JSON Schema 2020-12 subset. It rejects references,
combinators, conditionals, extension keywords other than ``x-nervos-immutable``, duplicate JSON
object keys, non-object roots, overly-large documents, and overly-deep structures before validation
can consume unbounded work.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import cast
from urllib.parse import urlparse

MAX_CONFIG_BYTES = 64 * 1024
MAX_DEPTH = 32
MAX_PATTERN_BYTES = 256
MAX_STRING_LENGTH = 16 * 1024
MAX_ARRAY_ITEMS = 1024
MAX_OBJECT_PROPERTIES = 256
MAX_ENUM_ENTRIES = 128

_JSON_PRIMITIVE = str | int | float | bool | None
JsonValue = _JSON_PRIMITIVE | list["JsonValue"] | dict[str, "JsonValue"]

_JSON_TYPES = {"array", "boolean", "integer", "null", "number", "object", "string"}
_ALLOWED_FORMATS = {"date-time", "date", "email", "hostname", "ipv4", "ipv6", "uri", "uuid"}
_ALLOWED_KEYWORDS = {
    "$schema",
    "additionalProperties",
    "const",
    "default",
    "description",
    "enum",
    "exclusiveMaximum",
    "exclusiveMinimum",
    "format",
    "items",
    "maximum",
    "maxItems",
    "maxLength",
    "maxProperties",
    "minimum",
    "minItems",
    "minLength",
    "minProperties",
    "multipleOf",
    "pattern",
    "properties",
    "required",
    "title",
    "type",
    "uniqueItems",
    "x-nervos-immutable",
}
_REJECTED_JSON_SCHEMA_KEYWORDS = {
    "$anchor",
    "$comment",
    "$defs",
    "$dynamicAnchor",
    "$dynamicRef",
    "$id",
    "$ref",
    "$vocabulary",
    "allOf",
    "anyOf",
    "contains",
    "dependentRequired",
    "dependentSchemas",
    "else",
    "if",
    "not",
    "oneOf",
    "patternProperties",
    "prefixItems",
    "propertyNames",
    "then",
    "unevaluatedItems",
    "unevaluatedProperties",
}


class PackageConfigSchemaError(ValueError):
    """Raised when a package configuration schema is outside the NervOS G1 subset."""


class PackageConfigValidationError(ValueError):
    """Raised when package configuration does not conform to an accepted schema."""


class _DuplicateKey(ValueError):
    """Internal sentinel raised by the JSON parser when an object key repeats."""


class _SchemaKind(StrEnum):
    ROOT = "root"
    PROPERTY = "property"
    ITEMS = "items"


@dataclass(frozen=True, slots=True)
class PackageConfigSchema:
    """A validated package configuration schema."""

    root: Mapping[str, JsonValue]


def parse_config_schema_json(raw: str | bytes | bytearray) -> PackageConfigSchema:
    """Parse and validate one raw JSON schema document."""
    parsed = _loads_bounded(raw, error_type=PackageConfigSchemaError)
    if not isinstance(parsed, dict):
        raise PackageConfigSchemaError("schema root must be a JSON object")
    return validate_config_schema(cast("Mapping[str, object]", parsed))


def validate_config_schema(schema: Mapping[str, object]) -> PackageConfigSchema:
    """Validate a parsed schema and return an immutable schema handle."""
    schema_copy = _json_deepcopy(schema)
    if not isinstance(schema_copy, dict):
        raise PackageConfigSchemaError("schema root must be a JSON object")
    _ensure_json_value(schema_copy, error_type=PackageConfigSchemaError)
    _ensure_depth_within_limit(schema_copy, error_type=PackageConfigSchemaError)
    _ensure_effective_size(schema_copy, error_type=PackageConfigSchemaError, subject="schema")
    _validate_schema_node(schema_copy, path="$", kind=_SchemaKind.ROOT)
    return PackageConfigSchema(root=cast("Mapping[str, JsonValue]", schema_copy))


def parse_config_json(raw: str | bytes | bytearray) -> dict[str, JsonValue]:
    """Parse one raw user configuration JSON document without applying schema defaults."""
    parsed = _loads_bounded(raw, error_type=PackageConfigValidationError)
    if not isinstance(parsed, dict):
        raise PackageConfigValidationError("configuration root must be a JSON object")
    parsed_config = cast("dict[str, object]", parsed)
    _ensure_json_value(parsed_config, error_type=PackageConfigValidationError)
    _ensure_depth_within_limit(parsed_config, error_type=PackageConfigValidationError)
    _ensure_effective_size(
        parsed_config, error_type=PackageConfigValidationError, subject="configuration"
    )
    return cast("dict[str, JsonValue]", parsed_config)


def validate_config(
    schema: PackageConfigSchema | Mapping[str, object], config: Mapping[str, object]
) -> dict[str, JsonValue]:
    """Return the effective configuration after applying defaults and validation.

    The caller's ``config`` object is never mutated. Defaults are copied into a new effective
    configuration in schema property order, which makes repeated validation deterministic.
    """
    schema_handle = (
        schema if isinstance(schema, PackageConfigSchema) else validate_config_schema(schema)
    )
    config_copy = _json_deepcopy(config)
    if not isinstance(config_copy, dict):
        raise PackageConfigValidationError("configuration root must be a JSON object")
    config_dict = cast("dict[str, object]", config_copy)
    _ensure_json_value(config_dict, error_type=PackageConfigValidationError)
    _ensure_depth_within_limit(config_dict, error_type=PackageConfigValidationError)
    _ensure_effective_size(
        config_dict, error_type=PackageConfigValidationError, subject="configuration"
    )

    effective = _apply_defaults(cast("Mapping[str, object]", schema_handle.root), config_dict)
    _ensure_depth_within_limit(effective, error_type=PackageConfigValidationError)
    _ensure_effective_size(
        effective, error_type=PackageConfigValidationError, subject="configuration"
    )
    _validate_instance(cast("Mapping[str, object]", schema_handle.root), effective, path="$")
    return cast("dict[str, JsonValue]", effective)


def _loads_bounded(raw: str | bytes | bytearray, *, error_type: type[ValueError]) -> object:
    if isinstance(raw, str):
        raw_bytes = raw.encode("utf-8")
        raw_text = raw
    else:
        raw_bytes = bytes(raw)
        try:
            raw_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise error_type("document must be UTF-8 JSON") from exc
    if len(raw_bytes) > MAX_CONFIG_BYTES:
        raise error_type("document exceeds 64KiB raw size limit")
    try:
        return json.loads(raw_text, object_pairs_hook=_reject_duplicate_keys)
    except _DuplicateKey as exc:
        raise error_type(str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise error_type("document must be valid JSON") from exc


def _reject_duplicate_keys(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _json_deepcopy(value: object) -> JsonValue:
    return cast("JsonValue", copy.deepcopy(value))


def _ensure_json_value(value: object, *, error_type: type[ValueError]) -> None:
    stack: list[object] = [value]
    while stack:
        current = stack.pop()
        if current is None or isinstance(current, str | bool):
            continue
        if isinstance(current, int):
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise error_type("numbers must be finite JSON values")
            continue
        if isinstance(current, list):
            stack.extend(cast("list[object]", current))
            continue
        if isinstance(current, dict):
            for key, item in cast("dict[object, object]", current).items():
                if not isinstance(key, str):
                    raise error_type("object keys must be strings")
                stack.append(item)
            continue
        raise error_type("document contains a non-JSON value")


def _ensure_depth_within_limit(value: object, *, error_type: type[ValueError]) -> None:
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > MAX_DEPTH:
            raise error_type("document exceeds maximum depth of 32")
        if isinstance(current, list):
            stack.extend((item, depth + 1) for item in cast("list[object]", current))
        elif isinstance(current, dict):
            stack.extend(
                (item, depth + 1) for item in cast("dict[object, object]", current).values()
            )


def _ensure_effective_size(value: object, *, error_type: type[ValueError], subject: str) -> None:
    size = len(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )
    if size > MAX_CONFIG_BYTES:
        raise error_type(f"{subject} exceeds 64KiB effective size limit")


def _validate_schema_node(schema: Mapping[str, object], *, path: str, kind: _SchemaKind) -> None:
    for keyword in schema:
        if keyword in _REJECTED_JSON_SCHEMA_KEYWORDS or keyword not in _ALLOWED_KEYWORDS:
            raise PackageConfigSchemaError(f"unsupported JSON Schema keyword at {path}: {keyword}")

    if schema.get("$schema") not in (None, "https://json-schema.org/draft/2020-12/schema"):
        raise PackageConfigSchemaError(f"unsupported $schema at {path}")

    schema_type = schema.get("type")
    if not isinstance(schema_type, str) or schema_type not in _JSON_TYPES:
        raise PackageConfigSchemaError(f"schema type at {path} must be one supported JSON type")
    if kind is _SchemaKind.ROOT and schema_type != "object":
        raise PackageConfigSchemaError("schema root type must be object")

    if "x-nervos-immutable" in schema:
        if kind is not _SchemaKind.PROPERTY:
            raise PackageConfigSchemaError("x-nervos-immutable is only valid on named properties")
        if not isinstance(schema["x-nervos-immutable"], bool):
            raise PackageConfigSchemaError("x-nervos-immutable must be boolean")

    _validate_metadata(schema, path=path)
    _validate_common_constraints(schema, path=path, schema_type=schema_type)

    if "default" in schema:
        _ensure_json_value(schema["default"], error_type=PackageConfigSchemaError)
        _ensure_depth_within_limit(schema["default"], error_type=PackageConfigSchemaError)

    properties = schema.get("properties")
    if properties is not None:
        if schema_type != "object" or not isinstance(properties, dict):
            raise PackageConfigSchemaError(f"properties at {path} must belong to an object schema")
        typed_properties = cast("dict[str, object]", properties)
        if len(typed_properties) > MAX_OBJECT_PROPERTIES:
            raise PackageConfigSchemaError(f"properties at {path} exceed limit")
        for name, child in typed_properties.items():
            if not isinstance(child, dict):
                raise PackageConfigSchemaError(
                    f"property schema at {path}.{name} must be an object"
                )
            _validate_schema_node(
                cast("Mapping[str, object]", child),
                path=f"{path}.properties.{name}",
                kind=_SchemaKind.PROPERTY,
            )

    required = schema.get("required")
    if required is not None:
        if schema_type != "object" or not isinstance(required, list):
            raise PackageConfigSchemaError(f"required at {path} must be an array on object schemas")
        typed_required = cast("list[object]", required)
        if not all(isinstance(item, str) for item in typed_required):
            raise PackageConfigSchemaError(f"required at {path} must contain unique property names")
        required_names = cast("list[str]", typed_required)
        if len(required_names) != len(set(required_names)):
            raise PackageConfigSchemaError(f"required at {path} must contain unique property names")
        known: set[str] = (
            set(cast("dict[str, object]", properties)) if isinstance(properties, dict) else set()
        )
        if not set(required_names).issubset(known):
            raise PackageConfigSchemaError(f"required at {path} may only name declared properties")

    additional = schema.get("additionalProperties")
    if additional is not None and not isinstance(additional, bool):
        raise PackageConfigSchemaError("additionalProperties must be boolean")

    items = schema.get("items")
    if items is not None:
        if schema_type != "array" or not isinstance(items, dict):
            raise PackageConfigSchemaError(f"items at {path} must be an object schema on arrays")
        _validate_schema_node(
            cast("Mapping[str, object]", items), path=f"{path}.items", kind=_SchemaKind.ITEMS
        )


def _validate_metadata(schema: Mapping[str, object], *, path: str) -> None:
    for key in ("title", "description"):
        value = schema.get(key)
        if value is not None and not isinstance(value, str):
            raise PackageConfigSchemaError(f"{key} at {path} must be a string")


def _validate_common_constraints(
    schema: Mapping[str, object], *, path: str, schema_type: str
) -> None:
    enum = schema.get("enum")
    if enum is not None:
        if not isinstance(enum, list):
            raise PackageConfigSchemaError(f"enum at {path} must be a bounded array")
        typed_enum = cast("list[object]", enum)
        if len(typed_enum) > MAX_ENUM_ENTRIES:
            raise PackageConfigSchemaError(f"enum at {path} must be a bounded array")
        for item in typed_enum:
            _ensure_json_value(item, error_type=PackageConfigSchemaError)

    if "const" in schema:
        _ensure_json_value(schema["const"], error_type=PackageConfigSchemaError)

    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"):
        value = schema.get(key)
        if value is not None:
            if schema_type not in {"integer", "number"} or not _is_finite_number(value):
                raise PackageConfigSchemaError(
                    f"{key} at {path} must be a finite numeric constraint"
                )
            if key == "multipleOf" and cast("float", value) <= 0:
                raise PackageConfigSchemaError(f"multipleOf at {path} must be positive")

    for key in ("minLength", "maxLength"):
        value = schema.get(key)
        if value is not None and (schema_type != "string" or not _is_non_negative_int(value)):
            raise PackageConfigSchemaError(
                f"{key} at {path} must be a non-negative integer on strings"
            )
    if (
        isinstance(schema.get("maxLength"), int)
        and cast("int", schema["maxLength"]) > MAX_STRING_LENGTH
    ):
        raise PackageConfigSchemaError(f"maxLength at {path} exceeds limit")

    pattern = schema.get("pattern")
    if pattern is not None:
        if schema_type != "string" or not isinstance(pattern, str):
            raise PackageConfigSchemaError(f"pattern at {path} must be a string constraint")
        _validate_safe_pattern(pattern, path=path)

    fmt = schema.get("format")
    if fmt is not None and (schema_type != "string" or fmt not in _ALLOWED_FORMATS):
        raise PackageConfigSchemaError(f"format at {path} is not supported")

    for key in ("minItems", "maxItems"):
        value = schema.get(key)
        if value is not None and (schema_type != "array" or not _is_non_negative_int(value)):
            raise PackageConfigSchemaError(
                f"{key} at {path} must be a non-negative integer on arrays"
            )
    if (
        isinstance(schema.get("maxItems"), int)
        and cast("int", schema["maxItems"]) > MAX_ARRAY_ITEMS
    ):
        raise PackageConfigSchemaError(f"maxItems at {path} exceeds limit")
    unique_items = schema.get("uniqueItems")
    if unique_items is not None and (schema_type != "array" or not isinstance(unique_items, bool)):
        raise PackageConfigSchemaError(f"uniqueItems at {path} must be boolean on arrays")

    for key in ("minProperties", "maxProperties"):
        value = schema.get(key)
        if value is not None and (schema_type != "object" or not _is_non_negative_int(value)):
            raise PackageConfigSchemaError(
                f"{key} at {path} must be a non-negative integer on objects"
            )
    if (
        isinstance(schema.get("maxProperties"), int)
        and cast("int", schema["maxProperties"]) > MAX_OBJECT_PROPERTIES
    ):
        raise PackageConfigSchemaError(f"maxProperties at {path} exceeds limit")


def _is_finite_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _is_non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_safe_pattern(pattern: str, *, path: str) -> None:
    if len(pattern.encode("utf-8")) > MAX_PATTERN_BYTES:
        raise PackageConfigSchemaError(f"pattern at {path} exceeds limit")
    forbidden_fragments = ("(?", "\\1", "\\2", "\\3", "\\4", "\\5", "\\6", "\\7", "\\8", "\\9")
    if any(fragment in pattern for fragment in forbidden_fragments):
        raise PackageConfigSchemaError(f"pattern at {path} uses unsupported regex features")
    repeated_quantifier = r"([*+?]|\{\d+(?:,\d*)?\})(?:[*+?]|\{\d+(?:,\d*)?\})"
    quantified_group = r"\([^()]*(?:[*+?]|\{\d+(?:,\d*)?\})[^()]*\)(?:[*+?]|\{\d+(?:,\d*)?\})"
    if re.search(repeated_quantifier, pattern) or re.search(quantified_group, pattern):
        raise PackageConfigSchemaError(f"pattern at {path} uses unsafe repeated quantifiers")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise PackageConfigSchemaError(f"pattern at {path} is not a valid regex") from exc


def _apply_defaults(schema: Mapping[str, object], config: dict[str, object]) -> dict[str, object]:
    effective = copy.deepcopy(config)
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, raw_child in cast("dict[str, object]", properties).items():
            child = cast("Mapping[str, object]", raw_child)
            if name not in effective and "default" in child:
                effective[name] = copy.deepcopy(child["default"])
            elif name in effective and isinstance(effective[name], dict):
                nested_type = child.get("type")
                if nested_type == "object":
                    effective[name] = _apply_defaults(
                        child, cast("dict[str, object]", effective[name])
                    )
    return effective


def _validate_instance(schema: Mapping[str, object], value: object, *, path: str) -> None:
    schema_type = cast("str", schema["type"])
    if not _matches_type(schema_type, value):
        raise PackageConfigValidationError(f"{path} must be {schema_type}")

    if "enum" in schema and value not in cast("list[object]", schema["enum"]):
        raise PackageConfigValidationError(f"{path} must match one enum value")
    if "const" in schema and value != schema["const"]:
        raise PackageConfigValidationError(f"{path} must match const value")

    if schema_type in {"number", "integer"}:
        _validate_numeric_constraints(schema, cast("int | float", value), path=path)
    elif schema_type == "string":
        _validate_string_constraints(schema, cast("str", value), path=path)
    elif schema_type == "array":
        _validate_array_constraints(schema, cast("list[object]", value), path=path)
    elif schema_type == "object":
        _validate_object_constraints(schema, cast("dict[str, object]", value), path=path)


def _matches_type(schema_type: str, value: object) -> bool:
    if schema_type == "null":
        return value is None
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return (
            isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
        )
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "object":
        return isinstance(value, dict)
    return False


def _validate_numeric_constraints(
    schema: Mapping[str, object], value: int | float, *, path: str
) -> None:
    if "minimum" in schema and value < cast("int | float", schema["minimum"]):
        raise PackageConfigValidationError(f"{path} is below minimum")
    if "maximum" in schema and value > cast("int | float", schema["maximum"]):
        raise PackageConfigValidationError(f"{path} is above maximum")
    if "exclusiveMinimum" in schema and value <= cast("int | float", schema["exclusiveMinimum"]):
        raise PackageConfigValidationError(f"{path} is not above exclusiveMinimum")
    if "exclusiveMaximum" in schema and value >= cast("int | float", schema["exclusiveMaximum"]):
        raise PackageConfigValidationError(f"{path} is not below exclusiveMaximum")
    if "multipleOf" in schema:
        quotient = value / cast("int | float", schema["multipleOf"])
        if not math.isclose(quotient, round(quotient), rel_tol=0, abs_tol=1e-12):
            raise PackageConfigValidationError(f"{path} is not a multipleOf value")


def _validate_string_constraints(schema: Mapping[str, object], value: str, *, path: str) -> None:
    if "minLength" in schema and len(value) < cast("int", schema["minLength"]):
        raise PackageConfigValidationError(f"{path} is shorter than minLength")
    if "maxLength" in schema and len(value) > cast("int", schema["maxLength"]):
        raise PackageConfigValidationError(f"{path} is longer than maxLength")
    if "pattern" in schema and re.search(cast("str", schema["pattern"]), value) is None:
        raise PackageConfigValidationError(f"{path} does not match pattern")
    if "format" in schema and not _matches_format(cast("str", schema["format"]), value):
        raise PackageConfigValidationError(f"{path} does not match format")


def _validate_array_constraints(
    schema: Mapping[str, object], value: list[object], *, path: str
) -> None:
    if "minItems" in schema and len(value) < cast("int", schema["minItems"]):
        raise PackageConfigValidationError(f"{path} has too few items")
    if "maxItems" in schema and len(value) > cast("int", schema["maxItems"]):
        raise PackageConfigValidationError(f"{path} has too many items")
    if schema.get("uniqueItems") is True:
        seen: set[str] = set()
        for item in value:
            canonical = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if canonical in seen:
                raise PackageConfigValidationError(f"{path} contains duplicate items")
            seen.add(canonical)
    items = schema.get("items")
    if isinstance(items, dict):
        for index, item in enumerate(value):
            _validate_instance(cast("Mapping[str, object]", items), item, path=f"{path}[{index}]")


def _validate_object_constraints(
    schema: Mapping[str, object], value: dict[str, object], *, path: str
) -> None:
    if "minProperties" in schema and len(value) < cast("int", schema["minProperties"]):
        raise PackageConfigValidationError(f"{path} has too few properties")
    if "maxProperties" in schema and len(value) > cast("int", schema["maxProperties"]):
        raise PackageConfigValidationError(f"{path} has too many properties")

    properties = schema.get("properties")
    known: set[str] = (
        set(cast("dict[str, object]", properties)) if isinstance(properties, dict) else set()
    )
    for name in cast("list[str]", schema.get("required", [])):
        if name not in value:
            raise PackageConfigValidationError(f"{path}.{name} is required")
    if schema.get("additionalProperties", True) is False:
        extra = set(value) - known
        if extra:
            first = sorted(extra)[0]
            raise PackageConfigValidationError(f"{path}.{first} is not an allowed property")
    if isinstance(properties, dict):
        for name, child in cast("dict[str, object]", properties).items():
            if name in value:
                _validate_instance(
                    cast("Mapping[str, object]", child), value[name], path=f"{path}.{name}"
                )


def _matches_format(fmt: str, value: str) -> bool:
    if fmt == "uuid":
        try:
            uuid.UUID(value)
        except ValueError:
            return False
        return True
    if fmt == "ipv4":
        try:
            ipaddress.IPv4Address(value)
        except ipaddress.AddressValueError:
            return False
        return True
    if fmt == "ipv6":
        try:
            ipaddress.IPv6Address(value)
        except ipaddress.AddressValueError:
            return False
        return True
    if fmt == "email":
        return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value))
    if fmt == "hostname":
        labels = value.rstrip(".").split(".")
        return bool(
            value
            and len(value) <= 253
            and all(
                0 < len(label) <= 63
                and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
                for label in labels
            )
        )
    if fmt == "uri":
        parsed = urlparse(value)
        return bool(parsed.scheme and (parsed.netloc or parsed.scheme == "urn"))
    if fmt == "date-time":
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return True
    if fmt == "date":
        try:
            datetime.fromisoformat(f"{value}T00:00:00")
        except ValueError:
            return False
        return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))
    return False
