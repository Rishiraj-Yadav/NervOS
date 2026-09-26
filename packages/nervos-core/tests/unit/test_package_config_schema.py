"""Stage G1 package configuration schema validation tests."""

import copy
import json

import pytest
from nervos_core.application.package_config_schema import (
    MAX_CONFIG_BYTES,
    JsonValue,
    PackageConfigSchemaError,
    PackageConfigValidationError,
    parse_config_json,
    parse_config_schema_json,
    validate_config,
    validate_config_schema,
)

VALID_SCHEMA: dict[str, JsonValue] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["endpoint"],
    "properties": {
        "endpoint": {"type": "string", "format": "uri", "x-nervos-immutable": True},
        "mode": {"type": "string", "enum": ["safe", "fast"], "default": "safe"},
        "retries": {"type": "integer", "minimum": 0, "maximum": 5, "default": 2},
        "labels": {
            "type": "array",
            "items": {"type": "string", "pattern": "^[a-z][a-z0-9_-]*$", "maxLength": 20},
            "maxItems": 4,
            "uniqueItems": True,
            "default": [],
        },
        "nested": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"enabled": {"type": "boolean", "default": True}},
            "default": {},
        },
    },
}


def test_valid_schema_and_config_apply_deterministic_defaults_without_mutating_input() -> None:
    schema_input = copy.deepcopy(VALID_SCHEMA)
    config_input: dict[str, JsonValue] = {"endpoint": "https://localhost:8443", "nested": {}}
    original_schema = copy.deepcopy(schema_input)
    original_config = copy.deepcopy(config_input)

    schema = validate_config_schema(schema_input)
    first = validate_config(schema, config_input)
    second = validate_config(schema, config_input)

    assert first == second
    assert first == {
        "endpoint": "https://localhost:8443",
        "mode": "safe",
        "retries": 2,
        "labels": [],
        "nested": {"enabled": True},
    }
    assert schema_input == original_schema
    assert config_input == original_config


def test_schema_and_config_parsing_reject_duplicate_json_keys() -> None:
    with pytest.raises(PackageConfigSchemaError, match="duplicate"):
        parse_config_schema_json('{"type":"object","type":"string"}')

    with pytest.raises(PackageConfigValidationError, match="duplicate"):
        parse_config_json('{"endpoint":"https://a.example","endpoint":"https://b.example"}')


def test_schema_and_config_roots_must_be_objects() -> None:
    with pytest.raises(PackageConfigSchemaError, match="root"):
        parse_config_schema_json("[]")

    with pytest.raises(PackageConfigValidationError, match="root"):
        parse_config_json("[]")

    with pytest.raises(PackageConfigSchemaError, match="root type"):
        validate_config_schema({"type": "array", "items": {"type": "string"}})


def test_raw_and_effective_size_limits_are_64_kib() -> None:
    with pytest.raises(PackageConfigSchemaError, match="64KiB raw"):
        parse_config_schema_json(" " * (MAX_CONFIG_BYTES + 1))

    many_defaults: dict[str, JsonValue] = {
        f"blob{index}": {"type": "string", "maxLength": 16384, "default": "x" * 16384}
        for index in range(5)
    }
    large_default_schema = {"type": "object", "properties": many_defaults}
    with pytest.raises(PackageConfigSchemaError, match="64KiB effective"):
        validate_config_schema(large_default_schema)

    schema = validate_config_schema({"type": "object", "properties": {"blob": {"type": "string"}}})
    with pytest.raises(PackageConfigValidationError, match="64KiB effective"):
        validate_config(schema, {"blob": "x" * MAX_CONFIG_BYTES})


def test_depth_limit_is_enforced_iteratively_for_schema_and_config() -> None:
    schema: dict[str, JsonValue] = {"type": "object", "properties": {}}
    cursor = schema["properties"]
    assert isinstance(cursor, dict)
    for index in range(40):
        child: dict[str, JsonValue] = {"type": "object", "properties": {}}
        cursor[f"p{index}"] = child
        next_cursor = child["properties"]
        assert isinstance(next_cursor, dict)
        cursor = next_cursor

    with pytest.raises(PackageConfigSchemaError, match="depth"):
        validate_config_schema(schema)

    config: dict[str, object] = {}
    current = config
    for index in range(40):
        child = {}
        current[f"p{index}"] = child
        current = child

    with pytest.raises(PackageConfigValidationError, match="depth"):
        parse_config_json(json.dumps(config))


def test_unsupported_json_schema_keywords_are_rejected_explicitly() -> None:
    for keyword in ("$ref", "allOf", "anyOf", "oneOf", "if", "then", "else", "$defs"):
        with pytest.raises(PackageConfigSchemaError, match="unsupported JSON Schema keyword"):
            validate_config_schema({"type": "object", keyword: []})


def test_fixed_format_allowlist_is_enforced() -> None:
    validate_config_schema(
        {"type": "object", "properties": {"id": {"type": "string", "format": "uuid"}}}
    )

    with pytest.raises(PackageConfigSchemaError, match="format"):
        validate_config_schema(
            {"type": "object", "properties": {"name": {"type": "string", "format": "regex"}}}
        )

    schema = validate_config_schema(
        {"type": "object", "properties": {"id": {"type": "string", "format": "uuid"}}}
    )
    with pytest.raises(PackageConfigValidationError, match="format"):
        validate_config(schema, {"id": "not-a-uuid"})


def test_pattern_policy_is_bounded_and_rejects_unsafe_regex_features() -> None:
    validate_config_schema(
        {"type": "object", "properties": {"name": {"type": "string", "pattern": "^[a-z0-9_-]+$"}}}
    )

    unsafe_patterns = ["(a+)+$", "(?=a)a", r"^(a)\1$", "x" * 257]
    for pattern in unsafe_patterns:
        with pytest.raises(PackageConfigSchemaError, match="pattern"):
            validate_config_schema(
                {"type": "object", "properties": {"name": {"type": "string", "pattern": pattern}}}
            )


def test_x_nervos_immutable_is_boolean_and_only_on_named_properties() -> None:
    validate_config_schema(
        {"type": "object", "properties": {"token": {"type": "string", "x-nervos-immutable": True}}}
    )

    with pytest.raises(PackageConfigSchemaError, match="x-nervos-immutable"):
        validate_config_schema({"type": "object", "x-nervos-immutable": True})

    with pytest.raises(PackageConfigSchemaError, match="x-nervos-immutable"):
        validate_config_schema(
            {
                "type": "object",
                "properties": {"token": {"type": "string", "x-nervos-immutable": "yes"}},
            }
        )


def test_config_validation_checks_required_unknown_types_arrays_and_numbers() -> None:
    schema = validate_config_schema(VALID_SCHEMA)

    with pytest.raises(PackageConfigValidationError, match="required"):
        validate_config(schema, {})

    with pytest.raises(PackageConfigValidationError, match="not an allowed property"):
        validate_config(schema, {"endpoint": "https://example.com", "extra": True})

    with pytest.raises(PackageConfigValidationError, match="maximum"):
        validate_config(schema, {"endpoint": "https://example.com", "retries": 6})

    with pytest.raises(PackageConfigValidationError, match="duplicate items"):
        validate_config(schema, {"endpoint": "https://example.com", "labels": ["ok", "ok"]})

    with pytest.raises(PackageConfigValidationError, match="pattern"):
        validate_config(schema, {"endpoint": "https://example.com", "labels": ["NotOk"]})


def test_separate_schema_and_config_apis_accept_mapping_inputs() -> None:
    schema_mapping: dict[str, JsonValue] = {
        "type": "object",
        "properties": {"enabled": {"type": "boolean", "default": False}},
    }
    schema = validate_config_schema(schema_mapping)

    assert validate_config(schema, {}) == {"enabled": False}
    assert validate_config(schema_mapping, {}) == {"enabled": False}
