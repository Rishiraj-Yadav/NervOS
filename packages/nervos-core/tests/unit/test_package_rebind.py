"""Unit tests for package configuration validation, carry-forward, and immutability checks."""

from __future__ import annotations

import pytest
from nervos_core.application.package_config_schema import (
    PackageConfigValidationError,
    check_immutable_fields,
    extract_immutable_property_names,
    parse_config_schema_json,
    validate_config,
)
from nervos_core.domain.package_query import ImmutableConfigViolation

SCHEMA_WITH_IMMUTABLE = b"""{
  "type": "object",
  "properties": {
    "account_id": {"type": "string", "x-nervos-immutable": true},
    "batch_size": {"type": "integer", "default": 10, "x-nervos-immutable": false}
  },
  "required": ["account_id"],
  "additionalProperties": false
}
"""

TARGET_SCHEMA_V2 = b"""{
  "type": "object",
  "properties": {
    "account_id": {"type": "string", "x-nervos-immutable": true},
    "batch_size": {"type": "integer", "default": 20, "x-nervos-immutable": false},
    "new_feature": {"type": "boolean", "default": true}
  },
  "required": ["account_id"],
  "additionalProperties": false
}
"""

TARGET_SCHEMA_INCOMPATIBLE = b"""{
  "type": "object",
  "properties": {
    "account_id": {"type": "string", "x-nervos-immutable": true},
    "new_required_field": {"type": "string"}
  },
  "required": ["account_id", "new_required_field"],
  "additionalProperties": false
}
"""


def test_extract_immutable_properties() -> None:
    schema = parse_config_schema_json(SCHEMA_WITH_IMMUTABLE)
    imm = extract_immutable_property_names(schema)
    assert imm == frozenset({"account_id"})


def test_immutable_field_modification_rejected() -> None:
    current = {"account_id": "ACC123", "batch_size": 10}
    new_cfg = {"account_id": "ACC999", "batch_size": 20}
    immutable_fields = frozenset({"account_id"})

    with pytest.raises(ImmutableConfigViolation, match="account_id"):
        check_immutable_fields(current, new_cfg, immutable_fields)


def test_mutable_field_modification_allowed() -> None:
    current = {"account_id": "ACC123", "batch_size": 10}
    new_cfg = {"account_id": "ACC123", "batch_size": 50}
    immutable_fields = frozenset({"account_id"})

    # Should not raise
    check_immutable_fields(current, new_cfg, immutable_fields)


def test_rebind_carry_forward_compatible() -> None:
    v1_config = {"account_id": "ACC123", "batch_size": 15}
    v2_schema = parse_config_schema_json(TARGET_SCHEMA_V2)

    # Validates and applies v2 defaults
    effective = validate_config(v2_schema, v1_config)
    assert effective["account_id"] == "ACC123"
    assert effective["batch_size"] == 15
    assert effective["new_feature"] is True


def test_rebind_carry_forward_incompatible_fails() -> None:
    v1_config = {"account_id": "ACC123"}
    incompat_schema = parse_config_schema_json(TARGET_SCHEMA_INCOMPATIBLE)

    with pytest.raises(PackageConfigValidationError):
        validate_config(incompat_schema, v1_config)
