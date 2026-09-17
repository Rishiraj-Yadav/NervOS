"""D3 tool domain tests: durable identity, model-facing names, and the definition fingerprint.

The naming algorithm is frozen in ADR 0015, and this suite pins the two clarifications D3 implements
as errata: the escape is over UTF-8 **bytes** (a fixed two-digit body is otherwise not injective
above ``U+00FF``), and the hash suffix is appended unconditionally so there is one code path for
both namespaces. The human-readable names in the ADR are labels, not persisted
``model_name`` values.
"""

from __future__ import annotations

import json

import pytest
from nervos_core.domain.tools import (
    MAX_MODEL_NAME_LENGTH,
    DefinitionStatus,
    InvalidToolDefinition,
    JsonValueReason,
    RiskHints,
    ToolDescriptor,
    ToolSourceKind,
    ToolSourceRef,
    canonical_json_text,
    definition_fingerprint,
    escape_upstream_name,
    model_tool_name,
    validate_json_value,
)

BUILTIN = ToolSourceRef(ToolSourceKind.BUILTIN, None)
INPUT_SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}}


def fingerprint_for(**overrides: object) -> str:
    base: dict[str, object] = {
        "model_name": "nervos__builtin__thing_0123456789ab",
        "upstream_name": "thing",
        "description": "A tool",
        "input_schema": INPUT_SCHEMA,
        "output_schema": None,
        "source_kind": ToolSourceKind.BUILTIN,
        "source_id": None,
        "risk_hints": RiskHints(),
    }
    base.update(overrides)
    return definition_fingerprint(**base)  # type: ignore[arg-type]


class TestEscaping:
    def test_simple_names_are_lowercased_and_pass_through(self) -> None:
        assert escape_upstream_name("read_file") == "read_5ffile"
        assert escape_upstream_name("Read-File") == "read-file"
        assert escape_upstream_name("v2") == "v2"

    def test_an_underscore_is_escaped_so_it_always_begins_one_escape(self) -> None:
        assert escape_upstream_name("_") == "_5f"
        assert escape_upstream_name("a_b") == "a_5fb"

    def test_punctuation_is_escaped_as_bytes(self) -> None:
        assert escape_upstream_name("read.file") == "read_2efile"
        assert escape_upstream_name("a/b") == "a_2fb"
        assert escape_upstream_name("a b") == "a_20b"

    def test_the_escape_is_injective_across_the_collision_classes(self) -> None:
        # These four would all collapse to one slug under a naive `[^a-z0-9_] -> _` substitution.
        variants = ["read.file", "read/file", "read file", "read-file"]
        assert len({escape_upstream_name(name) for name in variants}) == len(variants)

    def test_the_escape_is_injective_for_non_ascii_input(self) -> None:
        # The defect this closes: with "2 hex digits of the code point", '€' (U+20AC) escapes to
        # exactly the same text as ' ' followed by 'ac', so two distinct upstream names would share
        # one model name and a fail-closed assembly would take a whole catalog down.
        assert escape_upstream_name("€") == "_e2_82_ac"
        assert escape_upstream_name(" ac") == "_20ac"
        assert escape_upstream_name("€") != escape_upstream_name(" ac")

    def test_a_non_ascii_name_is_provider_safe(self) -> None:
        escaped = escape_upstream_name("café €")
        assert escaped.isascii()
        assert all(character.isalnum() or character in "-_" for character in escaped)


class TestModelToolName:
    def test_a_builtin_name_uses_the_builtin_namespace(self) -> None:
        name = model_tool_name(BUILTIN, "current_time")
        assert name.startswith("nervos__builtin__")

    def test_an_mcp_name_carries_the_connection_id(self) -> None:
        assert model_tool_name(ToolSourceRef(ToolSourceKind.MCP, 12), "t").startswith(
            "nervos__c12__"
        )

    def test_builtin_and_mcp_namespaces_cannot_collide(self) -> None:
        names = {
            model_tool_name(BUILTIN, "t"),
            model_tool_name(ToolSourceRef(ToolSourceKind.MCP, 1), "t"),
        }
        assert len(names) == 2

    def test_two_connections_of_the_same_kind_get_distinct_names(self) -> None:
        first = model_tool_name(ToolSourceRef(ToolSourceKind.MCP, 12), "read_file")
        second = model_tool_name(ToolSourceRef(ToolSourceKind.MCP, 27), "read_file")
        assert first != second

    def test_the_suffix_is_always_appended(self) -> None:
        # One code path for both namespaces: the suffix is not conditional on truncation.
        name = model_tool_name(BUILTIN, "current_time")
        assert name != "nervos__builtin__current_5ftime"
        assert len(name.rsplit("_", 1)[1]) == 12

    def test_naming_is_deterministic(self) -> None:
        assert model_tool_name(BUILTIN, "x") == model_tool_name(BUILTIN, "x")

    def test_a_long_name_is_truncated_to_exactly_the_bound(self) -> None:
        name = model_tool_name(BUILTIN, "a" * 200)
        assert len(name) == MAX_MODEL_NAME_LENGTH

    def test_long_names_sharing_a_prefix_stay_distinct(self) -> None:
        first = model_tool_name(BUILTIN, "a" * 100 + "one")
        second = model_tool_name(BUILTIN, "a" * 100 + "two")
        assert first != second

    def test_every_generated_name_is_provider_safe(self) -> None:
        names = [
            model_tool_name(BUILTIN, "current_time"),
            model_tool_name(BUILTIN, "a" * 200),
            model_tool_name(ToolSourceRef(ToolSourceKind.MCP, 999999), "read.file"),
            model_tool_name(ToolSourceRef(ToolSourceKind.MCP, 7), "café"),
        ]
        for name in names:
            assert 1 <= len(name) <= MAX_MODEL_NAME_LENGTH
            assert name == name.lower()
            assert name.isascii()
            assert name[0].isalnum()
            assert all(character.isalnum() or character in "-_" for character in name)


class TestToolSourceRef:
    def test_a_builtin_source_has_no_source_id(self) -> None:
        assert ToolSourceRef(ToolSourceKind.BUILTIN, None).is_builtin is True

    def test_a_builtin_source_cannot_carry_a_source_id(self) -> None:
        with pytest.raises(InvalidToolDefinition):
            ToolSourceRef(ToolSourceKind.BUILTIN, 7)

    def test_an_mcp_source_requires_a_positive_id(self) -> None:
        with pytest.raises(InvalidToolDefinition):
            ToolSourceRef(ToolSourceKind.MCP, None)
        with pytest.raises(InvalidToolDefinition):
            ToolSourceRef(ToolSourceKind.MCP, 0)
        assert ToolSourceRef(ToolSourceKind.MCP, 3).source_id == 3

    def test_a_source_ref_is_hashable_so_registries_can_key_on_it(self) -> None:
        assert (
            len({ToolSourceRef(ToolSourceKind.MCP, 1), ToolSourceRef(ToolSourceKind.MCP, 1)}) == 1
        )


class TestToolDescriptor:
    def descriptor(self, **overrides: object) -> ToolDescriptor:
        base: dict[str, object] = {
            "tool_definition_id": 1,
            "upstream_name": "thing",
            "model_name": model_tool_name(BUILTIN, "thing"),
            "source_kind": ToolSourceKind.BUILTIN,
            "source_id": None,
            "display_name": "Thing",
            "description": "A tool",
            "input_schema": dict(INPUT_SCHEMA),
            "output_schema": None,
            "risk_hints": RiskHints(),
            "fingerprint": "a" * 64,
        }
        base.update(overrides)
        return ToolDescriptor(**base)  # type: ignore[arg-type]

    def test_a_durable_descriptor_carries_its_real_id(self) -> None:
        assert self.descriptor(tool_definition_id=42).tool_definition_id == 42

    def test_a_descriptor_without_a_durable_identity_cannot_be_constructed(self) -> None:
        # There is no unpersisted form of this type, so no consumer ever asserts the id is present.
        with pytest.raises(InvalidToolDefinition):
            self.descriptor(tool_definition_id=0)
        with pytest.raises(InvalidToolDefinition):
            self.descriptor(tool_definition_id=-1)

    def test_the_source_ref_is_derived_not_stored(self) -> None:
        assert self.descriptor().source_ref == BUILTIN

    def test_a_malformed_model_name_is_refused(self) -> None:
        for bad in ("Upper", "has space", "a" * 65, "_leading", "dot.ted"):
            with pytest.raises(InvalidToolDefinition):
                self.descriptor(model_name=bad)

    def test_a_malformed_fingerprint_is_refused(self) -> None:
        with pytest.raises(InvalidToolDefinition):
            self.descriptor(fingerprint="too-short")
        with pytest.raises(InvalidToolDefinition):
            self.descriptor(fingerprint="A" * 64)

    def test_a_malformed_display_name_is_refused(self) -> None:
        with pytest.raises(InvalidToolDefinition):
            self.descriptor(display_name=" untrimmed")
        with pytest.raises(InvalidToolDefinition):
            self.descriptor(display_name="")


class TestFingerprint:
    def test_a_fingerprint_is_a_sha256_hex(self) -> None:
        value = fingerprint_for()
        assert len(value) == 64
        assert all(character in "0123456789abcdef" for character in value)

    def test_identical_inputs_give_identical_fingerprints(self) -> None:
        assert fingerprint_for() == fingerprint_for()

    def test_semantically_identical_schemas_fingerprint_identically(self) -> None:
        # Two spellings of one schema, parsed as JSON. Key order and whitespace are not meaning.
        compact = json.loads('{"type":"object","properties":{"a":{"type":"string"}}}')
        spaced = json.loads('{ "properties" : { "a" : { "type" : "string" } }, "type" : "object" }')
        assert fingerprint_for(input_schema=compact) == fingerprint_for(input_schema=spaced)

    @pytest.mark.parametrize(
        "field, value",
        [
            ("model_name", "nervos__builtin__other_0123456789ab"),
            ("upstream_name", "other"),
            ("description", "A different tool"),
            ("input_schema", {"type": "object", "properties": {"b": {"type": "string"}}}),
            ("output_schema", {"type": "object", "properties": {}}),
            ("source_kind", ToolSourceKind.MCP),
            ("source_id", 4),
            ("risk_hints", RiskHints(read_only=True, destructive=True)),
            ("risk_hints", RiskHints(idempotent=True)),
            ("risk_hints", RiskHints(open_world=False)),
        ],
    )
    def test_every_material_field_changes_the_fingerprint(self, field: str, value: object) -> None:
        assert fingerprint_for(**{field: value}) != fingerprint_for()

    def test_the_canonical_form_is_sorted_and_whitespace_free(self) -> None:
        assert (
            canonical_json_text({"b": 1, "a": [1, {"d": 2, "c": 3}]})
            == '{"a":[1,{"c":3,"d":2}],"b":1}'
        )


class TestJsonContractRejections:
    """`JsonValue` is the contract; `object` is only what a validator classifies."""

    @pytest.mark.parametrize(
        "value",
        [float("inf"), b"x", object(), {1: 2}, (1,), {1}],
    )
    def test_values_outside_the_contract_are_rejected(self, value: object) -> None:
        assert validate_json_value(value) is not None

    def test_a_non_string_mapping_key_is_named_as_such(self) -> None:
        rejection = validate_json_value({"ok": {1: "bad"}})
        assert rejection is not None
        assert rejection.reason is JsonValueReason.NON_STRING_KEY


class TestRiskHints:
    def test_the_defaults_are_the_conservative_reading(self) -> None:
        hints = RiskHints()
        assert hints.read_only is False
        assert hints.destructive is True
        assert hints.idempotent is False
        assert hints.open_world is True

    def test_definition_status_covers_the_frozen_vocabulary(self) -> None:
        assert {status.value for status in DefinitionStatus} == {
            "available",
            "unavailable",
            "unsupported_schema",
        }
