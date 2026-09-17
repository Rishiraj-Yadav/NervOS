"""D3 canonical schema tests: the supported subset, and instance validation over it.

Two separate questions are proven here. Schema validation answers *is this schema inside the
canonical subset*; instance validation answers *do these values conform to one that is*. Neither
resolves a reference, coerces a value, drops an unknown field, or weakens an unsupported construct.

The byte ceiling is deliberately **not** universal: it applies only when the caller supplies one,
because D4 must be able to enforce a Run's own ``tool_result_max_bytes`` exactly.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from nervos_core.application.tool_registry import ToolResult
from nervos_core.application.tool_schema import (
    ARRAY,
    INTEGER,
    NUMBER,
    OBJECT,
    CanonicalSchema,
    InstanceLimits,
    InstanceRejectionReason,
    SchemaRejection,
    SchemaRejectionReason,
    canonical_json_size,
    validate_canonical_schema,
    validate_instance,
)
from nervos_core.domain.tools import (
    InvalidToolDefinition,
    JsonValueReason,
    validate_json_value,
)

# Every construct ADR 0015 rejects outright, named so a rejection must say *which* keyword.
REJECTED_KEYWORDS = [
    "$ref",
    "oneOf",
    "anyOf",
    "allOf",
    "not",
    "if",
    "then",
    "else",
    "patternProperties",
    "dependentSchemas",
    "dependentRequired",
    "propertyNames",
    "const",
    "format",
    "pattern",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "multipleOf",
    "minItems",
    "maxItems",
    "uniqueItems",
    "prefixItems",
    "contains",
    "unevaluatedProperties",
    "$defs",
    "definitions",
    "$schema",
    "$id",
    "title",
    "default",
    "examples",
]


def accepted(schema: object) -> CanonicalSchema:
    result = validate_canonical_schema(schema)
    assert isinstance(result, CanonicalSchema), result
    return result


def rejected(schema: object) -> SchemaRejection:
    result = validate_canonical_schema(schema)
    assert isinstance(result, SchemaRejection), result
    return result


def nested_object(depth: int) -> dict[str, object]:
    """A root object whose deepest node sits at exactly ``depth`` (the root is depth 1)."""
    node: dict[str, object] = {"type": "string"}
    for _ in range(depth - 1):
        node = {"type": OBJECT, "properties": {"x": node}}
    return node


class TestAcceptedSchemas:
    def test_a_minimal_object_schema_is_accepted(self) -> None:
        schema = accepted({"type": OBJECT, "properties": {}})
        assert schema.node_count == 1

    def test_the_full_supported_subset_is_accepted(self) -> None:
        schema = accepted(
            {
                "type": OBJECT,
                "description": "Every admitted construct in one schema.",
                "properties": {
                    "text": {"type": "string", "enum": ["a", "b"]},
                    "count": {"type": "integer", "enum": [1, 2]},
                    "ratio": {"type": "number"},
                    "flag": {"type": "boolean"},
                    "nested": {
                        "type": OBJECT,
                        "properties": {"deep": {"type": "string"}},
                        "required": ["deep"],
                        "additionalProperties": False,
                    },
                    "items": {"type": ARRAY, "items": {"type": "string"}},
                },
                "required": ["text"],
                "additionalProperties": False,
            }
        )
        assert schema.node_count == 9

    def test_an_open_object_is_accepted_and_additional_properties_is_optional(self) -> None:
        # `additionalProperties` absent is the open-object case the built-ins rely on.
        schema = accepted(
            {"type": OBJECT, "properties": {"doc": {"type": OBJECT, "properties": {}}}}
        )
        assert schema.root["properties"] is not None


class TestRejectedSchemas:
    @pytest.mark.parametrize("keyword", REJECTED_KEYWORDS)
    def test_every_unsupported_keyword_is_rejected_by_name(self, keyword: str) -> None:
        rejection = rejected(
            {"type": OBJECT, "properties": {"a": {"type": "string", keyword: "anything"}}}
        )
        assert rejection.reason is SchemaRejectionReason.UNSUPPORTED_KEYWORD
        assert rejection.detail == keyword

    def test_a_reference_is_never_resolved_it_is_rejected(self) -> None:
        rejection = rejected({"type": OBJECT, "properties": {"a": {"$ref": "#/$defs/thing"}}})
        assert rejection.reason is SchemaRejectionReason.UNSUPPORTED_KEYWORD
        assert rejection.detail == "$ref"

    def test_a_reference_node_without_a_type_still_names_the_reference(self) -> None:
        # The realistic shape: a `$ref` substitute carries no `type` at all, so the reason must be
        # about the keyword rather than a misleading "malformed type".
        rejection = rejected({"type": OBJECT, "properties": {"a": {"$ref": "#/$defs/thing"}}})
        assert rejection.detail == "$ref"
        assert rejected({"$ref": "#/$defs/thing"}).detail == "$ref"

    def test_a_keyword_valid_for_another_type_is_rejected(self) -> None:
        # `items` belongs to an array node, `properties` to an object node. Neither is a no-op.
        assert (
            rejected(
                {
                    "type": OBJECT,
                    "properties": {"a": {"type": "string", "items": {"type": "string"}}},
                }
            ).detail
            == "items"
        )
        assert (
            rejected(
                {
                    "type": OBJECT,
                    "properties": {
                        "a": {"type": ARRAY, "items": {"type": "string"}, "properties": {}}
                    },
                }
            ).detail
            == "properties"
        )

    def test_a_non_object_root_is_rejected(self) -> None:
        assert rejected({"type": "string"}).reason is SchemaRejectionReason.ROOT_NOT_OBJECT
        assert rejected("not a schema").reason is SchemaRejectionReason.NOT_AN_OBJECT
        assert rejected([1, 2]).reason is SchemaRejectionReason.NOT_AN_OBJECT

    def test_a_missing_or_malformed_type_is_rejected(self) -> None:
        assert rejected({"properties": {}}).reason is SchemaRejectionReason.MALFORMED_TYPE
        assert rejected({"type": "any", "properties": {}}).reason is (
            SchemaRejectionReason.MALFORMED_TYPE
        )

    def test_properties_must_be_present_and_an_object(self) -> None:
        assert rejected({"type": OBJECT}).reason is SchemaRejectionReason.PROPERTIES_MISSING
        assert rejected({"type": OBJECT, "properties": []}).reason is (
            SchemaRejectionReason.PROPERTIES_NOT_OBJECT
        )

    def test_required_must_be_a_list_of_declared_unique_properties(self) -> None:
        base = {"type": OBJECT, "properties": {"a": {"type": "string"}}}
        assert rejected({**base, "required": "a"}).reason is (
            SchemaRejectionReason.REQUIRED_NOT_LIST
        )
        assert rejected({**base, "required": ["missing"]}).reason is (
            SchemaRejectionReason.REQUIRED_INCONSISTENT
        )
        assert rejected({**base, "required": ["a", "a"]}).reason is (
            SchemaRejectionReason.REQUIRED_INCONSISTENT
        )
        assert accepted({**base, "required": ["a"]}).node_count == 2

    def test_additional_properties_true_is_rejected_rather_than_widening(self) -> None:
        rejection = rejected({"type": OBJECT, "properties": {}, "additionalProperties": True})
        assert rejection.reason is SchemaRejectionReason.ADDITIONAL_PROPERTIES_NOT_FALSE
        # `0` is not `False` either: the check is identity, not truthiness.
        assert rejected({"type": OBJECT, "properties": {}, "additionalProperties": 0}).reason is (
            SchemaRejectionReason.ADDITIONAL_PROPERTIES_NOT_FALSE
        )

    def test_an_array_requires_items(self) -> None:
        assert rejected({"type": OBJECT, "properties": {"a": {"type": ARRAY}}}).reason is (
            SchemaRejectionReason.ITEMS_MISSING
        )

    def test_enum_entries_must_match_the_declared_type(self) -> None:
        base: dict[str, object] = {"type": OBJECT, "properties": {}}
        assert (
            rejected({**base, "properties": {"a": {"type": "string", "enum": [1]}}}).reason
            is SchemaRejectionReason.MALFORMED_ENUM
        )
        assert (
            rejected({**base, "properties": {"a": {"type": "integer", "enum": [True]}}}).reason
            is SchemaRejectionReason.MALFORMED_ENUM
        )
        assert (
            rejected({**base, "properties": {"a": {"type": "string", "enum": ["a", "a"]}}}).reason
            is SchemaRejectionReason.MALFORMED_ENUM
        )
        assert (
            rejected({**base, "properties": {"a": {"type": "string", "enum": []}}}).reason
            is SchemaRejectionReason.MALFORMED_ENUM
        )

    def test_enum_is_bounded_to_32_entries(self) -> None:
        entries = [f"v{index}" for index in range(33)]
        rejection = rejected(
            {"type": OBJECT, "properties": {"a": {"type": "string", "enum": entries}}}
        )
        assert rejection.reason is SchemaRejectionReason.MALFORMED_ENUM

    def test_the_schema_depth_bound_holds_on_both_sides(self) -> None:
        assert accepted(nested_object(8)).node_count == 8
        assert rejected(nested_object(9)).reason is SchemaRejectionReason.DEPTH_EXCEEDED

    def test_a_pathologically_deep_document_is_still_reported_as_a_depth_problem(self) -> None:
        # The pre-flight must never mask the schema depth verdict with a malformed-document one.
        assert rejected(nested_object(60)).reason is SchemaRejectionReason.DEPTH_EXCEEDED

    def test_the_schema_node_bound_holds_on_both_sides(self) -> None:
        at_limit = {f"p{index}": {"type": "string"} for index in range(127)}
        assert accepted({"type": OBJECT, "properties": at_limit}).node_count == 128
        over_limit = {f"p{index}": {"type": "string"} for index in range(128)}
        assert rejected({"type": OBJECT, "properties": over_limit}).reason is (
            SchemaRejectionReason.NODE_LIMIT_EXCEEDED
        )

    def test_the_schema_byte_bound_is_enforced_on_the_total_document(self) -> None:
        # Each description is inside the per-description bound; together they exceed the document's.
        chunk = "x" * 40000
        rejection = rejected(
            {
                "type": OBJECT,
                "properties": {
                    "a": {"type": "string", "description": chunk},
                    "b": {"type": "string", "description": chunk},
                },
            }
        )
        assert rejection.reason is SchemaRejectionReason.BYTES_EXCEEDED

    def test_a_description_beyond_its_own_bound_is_rejected(self) -> None:
        rejection = rejected(
            {"type": OBJECT, "properties": {"a": {"type": "string", "description": "x" * 70000}}}
        )
        assert rejection.reason is SchemaRejectionReason.MALFORMED_DESCRIPTION
        assert rejected({"type": OBJECT, "description": 5, "properties": {}}).reason is (
            SchemaRejectionReason.MALFORMED_DESCRIPTION
        )


class TestInstanceTyping:
    def schema_for(self, node: Mapping[str, object]) -> CanonicalSchema:
        return accepted({"type": OBJECT, "properties": {"v": node}})

    def test_an_integer_satisfies_number_but_a_float_does_not_satisfy_integer(self) -> None:
        number = self.schema_for({"type": NUMBER})
        integer = self.schema_for({"type": INTEGER})
        assert validate_instance(number, {"v": 5}) is None
        assert validate_instance(integer, {"v": 5}) is None
        rejection = validate_instance(integer, {"v": 5.0})
        assert rejection is not None and rejection.reason is InstanceRejectionReason.TYPE_MISMATCH

    def test_boolean_is_never_a_number_or_an_integer(self) -> None:
        for node in ({"type": NUMBER}, {"type": INTEGER}):
            schema = self.schema_for(node)
            for value in (True, False):
                rejection = validate_instance(schema, {"v": value})
                assert rejection is not None
                assert rejection.reason is InstanceRejectionReason.TYPE_MISMATCH

    def test_strings_are_never_coerced_into_numbers_or_flags(self) -> None:
        assert validate_instance(self.schema_for({"type": INTEGER}), {"v": "5"}) is not None
        assert validate_instance(self.schema_for({"type": "boolean"}), {"v": "true"}) is not None
        assert validate_instance(self.schema_for({"type": "string"}), {"v": 5}) is not None

    def test_non_finite_numbers_are_rejected(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            rejection = validate_instance(self.schema_for({"type": NUMBER}), {"v": value})
            assert rejection is not None

    def test_enum_equality_is_type_aware(self) -> None:
        # `True == 1` in Python, so a naive membership test would accept both of these.
        flags = self.schema_for({"type": "boolean", "enum": [True]})
        assert validate_instance(flags, {"v": True}) is None
        assert validate_instance(flags, {"v": 1}) is not None

        numbers = self.schema_for({"type": INTEGER, "enum": [1]})
        assert validate_instance(numbers, {"v": 1}) is None
        assert validate_instance(numbers, {"v": True}) is not None

    def test_enum_mismatch_is_reported_as_such(self) -> None:
        schema = self.schema_for({"type": "string", "enum": ["a", "b"]})
        rejection = validate_instance(schema, {"v": "c"})
        assert rejection is not None
        assert rejection.reason is InstanceRejectionReason.ENUM_MISMATCH

    def test_required_and_nested_objects_are_enforced(self) -> None:
        schema = accepted(
            {
                "type": OBJECT,
                "properties": {
                    "outer": {
                        "type": OBJECT,
                        "properties": {"inner": {"type": "integer"}},
                        "required": ["inner"],
                        "additionalProperties": False,
                    }
                },
                "required": ["outer"],
            }
        )
        assert validate_instance(schema, {"outer": {"inner": 1}}) is None
        missing = validate_instance(schema, {"outer": {}})
        assert missing is not None and missing.reason is InstanceRejectionReason.MISSING_REQUIRED
        assert missing.path == "$.outer"
        assert validate_instance(schema, {}) is not None

    def test_arrays_are_traversed_with_their_index_in_the_path(self) -> None:
        schema = self.schema_for({"type": ARRAY, "items": {"type": "integer"}})
        assert validate_instance(schema, {"v": [1, 2, 3]}) is None
        rejection = validate_instance(schema, {"v": [1, "two"]})
        assert rejection is not None
        assert rejection.path == "$.v[1]"


class TestOpenAndClosedObjects:
    def open_schema(self) -> CanonicalSchema:
        return accepted({"type": OBJECT, "properties": {"doc": {"type": OBJECT, "properties": {}}}})

    def closed_schema(self) -> CanonicalSchema:
        return accepted(
            {
                "type": OBJECT,
                "properties": {
                    "doc": {"type": OBJECT, "properties": {}, "additionalProperties": False}
                },
            }
        )

    def test_an_open_object_permits_unknown_keys_and_keeps_them(self) -> None:
        value = {"doc": {"anything": [1, {"nested": None}], "more": "x"}}
        assert validate_instance(self.open_schema(), value) is None
        # Nothing is dropped and nothing is rewritten.
        assert value == {"doc": {"anything": [1, {"nested": None}], "more": "x"}}

    def test_a_closed_object_rejects_an_unknown_key(self) -> None:
        rejection = validate_instance(self.closed_schema(), {"doc": {"anything": 1}})
        assert rejection is not None
        assert rejection.reason is InstanceRejectionReason.UNKNOWN_PROPERTY
        assert rejection.detail == "anything"

    def test_unknown_values_are_still_subject_to_the_depth_bound(self) -> None:
        deep: object = 1
        for _ in range(20):
            deep = {"nested": deep}
        rejection = validate_instance(self.open_schema(), {"doc": deep})
        assert rejection is not None
        assert rejection.reason is InstanceRejectionReason.DEPTH_EXCEEDED


class TestInstanceByteLimits:
    """The byte ceiling is context-supplied. It is never a universal property of validation."""

    def wide(self) -> tuple[CanonicalSchema, dict[str, object]]:
        schema = accepted({"type": OBJECT, "properties": {"blob": {"type": "string"}}})
        return schema, {"blob": "x" * 80000}

    def test_no_byte_ceiling_is_applied_by_default(self) -> None:
        schema, value = self.wide()
        assert canonical_json_size(value) > 65536  # type: ignore[arg-type]
        assert validate_instance(schema, value) is None

    def test_a_supplied_ceiling_is_enforced_exactly(self) -> None:
        schema, value = self.wide()
        limits = InstanceLimits(max_bytes=65536)
        rejection = validate_instance(schema, value, limits=limits)
        assert rejection is not None
        assert rejection.reason is InstanceRejectionReason.BYTES_EXCEEDED
        # The same value passes under a ceiling that admits it, proving the bound is the caller's.
        assert validate_instance(schema, value, limits=InstanceLimits(max_bytes=200000)) is None

    def test_the_byte_helper_measures_without_deciding(self) -> None:
        assert canonical_json_size({"a": 1}) == len(b'{"a":1}')

    def test_a_supplied_depth_bound_is_honoured(self) -> None:
        schema = accepted({"type": OBJECT, "properties": {"v": {"type": OBJECT, "properties": {}}}})
        value = {"v": {"a": {"b": {"c": 1}}}}
        assert validate_instance(schema, value, limits=InstanceLimits(max_depth=8)) is None
        rejection = validate_instance(schema, value, limits=InstanceLimits(max_depth=2))
        assert rejection is not None
        assert rejection.reason is InstanceRejectionReason.DEPTH_EXCEEDED


class TestJsonValueContract:
    @pytest.mark.parametrize(
        "value",
        [
            float("nan"),
            float("inf"),
            float("-inf"),
            b"bytes",
            bytearray(b"bytes"),
            (1, 2),
            {1, 2},
            frozenset({1}),
            {1: "int key"},
        ],
    )
    def test_values_outside_the_json_contract_are_rejected(self, value: object) -> None:
        rejection = validate_json_value(value)
        assert rejection is not None

    @pytest.mark.parametrize(
        "value",
        [None, True, 0, 1.5, "text", [1, "a", None], {"a": [1, {"b": None}]}],
    )
    def test_values_inside_the_json_contract_are_accepted(self, value: object) -> None:
        assert validate_json_value(value) is None

    def test_a_non_dict_mapping_is_not_part_of_the_contract(self) -> None:
        # The canonical form is JSON, and JSON objects are dicts. Nothing is silently normalised.
        from collections import UserDict

        assert validate_json_value(UserDict({"a": 1})) is not None

    def test_a_nested_violation_reports_its_path(self) -> None:
        rejection = validate_json_value({"a": [1, {"b": float("nan")}]})
        assert rejection is not None
        assert rejection.reason is JsonValueReason.NON_FINITE_NUMBER
        assert rejection.path == "$.a[1].b"

    def test_a_deep_document_is_rejected_rather_than_recursing_without_bound(self) -> None:
        deep: object = 1
        for _ in range(40):
            deep = {"nested": deep}
        rejection = validate_json_value(deep)
        assert rejection is not None
        assert rejection.reason is JsonValueReason.DEPTH_EXCEEDED

    def test_tool_result_refuses_a_structured_value_outside_the_contract(self) -> None:
        assert ToolResult(text="ok", structured={"fine": [1, 2]}).structured == {"fine": [1, 2]}
        with pytest.raises(InvalidToolDefinition):
            ToolResult(text="bad", structured={"when": object()})  # type: ignore[dict-item]
