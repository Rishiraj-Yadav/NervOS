"""D3 built-in tool tests: `current_time`, `calculate`, and `json_transform`.

All three are side-effect-free and credential-free, and none touches the filesystem, a subprocess, a
shell, the network, an environment variable or the database. `calculate` and `json_transform` are
pure functions of their arguments; `current_time` deliberately is **not** -- it depends on its
injected clock, so two calls with identical arguments legitimately differ.
"""

from __future__ import annotations

import asyncio
import decimal
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import pytest
from nervos_core.application.builtin_tools import (
    MAX_EXPRESSION_LENGTH,
    BuiltinToolExecutor,
    BuiltinToolSpec,
    builtin_tool_specs,
    canonical_spec_schemas,
)
from nervos_core.application.tool_registry import (
    ToolExecutionFailure,
    ToolFailureReason,
    ToolResult,
)
from nervos_core.application.tool_schema import CanonicalSchema, validate_instance
from nervos_core.domain.tools import (
    JsonValue,
    RiskHints,
    ToolDescriptor,
    ToolSourceKind,
    ToolSourceRef,
    model_tool_name,
)

BUILTIN_SOURCE_REF = ToolSourceRef(ToolSourceKind.BUILTIN, None)
FIXED_INSTANT = datetime(2026, 9, 17, 14, 49, 3, tzinfo=UTC)
FIXED_UNIX_SECONDS = 1789656543
SPECS = builtin_tool_specs(clock=lambda: FIXED_INSTANT)


def descriptor_for(upstream_name: str, tool_definition_id: int = 1) -> ToolDescriptor:
    return ToolDescriptor(
        tool_definition_id=tool_definition_id,
        upstream_name=upstream_name,
        model_name=model_tool_name(BUILTIN_SOURCE_REF, upstream_name),
        source_kind=ToolSourceKind.BUILTIN,
        source_id=None,
        display_name=upstream_name,
        description="A built-in",
        input_schema={},
        output_schema=None,
        risk_hints=RiskHints(),
        fingerprint="a" * 64,
    )


def call(tool: str, arguments: Mapping[str, JsonValue]) -> ToolResult:
    executor = BuiltinToolExecutor(SPECS)
    return asyncio.run(executor.execute(descriptor_for(tool), arguments))


def failure(tool: str, arguments: Mapping[str, JsonValue]) -> ToolExecutionFailure:
    with pytest.raises(ToolExecutionFailure) as caught:
        call(tool, arguments)
    return caught.value


def spec_for(upstream_name: str) -> BuiltinToolSpec:
    return next(spec for spec in SPECS if spec.upstream_name == upstream_name)


class TestCurrentTime:
    def test_utc_is_the_default(self) -> None:
        result = call("current_time", {})
        assert result.structured == {
            "iso8601": "2026-09-17T14:49:03+00:00",
            "unix_seconds": FIXED_UNIX_SECONDS,
            "timezone": "UTC",
        }

    @pytest.mark.parametrize(
        "requested, rendered, reported",
        [
            ("+05:30", "+05:30", "+05:30"),
            ("+05:45", "+05:45", "+05:45"),
            ("+12:45", "+12:45", "+12:45"),
            ("+14:00", "+14:00", "+14:00"),
            ("-14:00", "-14:00", "-14:00"),
            # A zero offset is reported canonically, so the three output fields cannot disagree.
            ("+00:00", "+00:00", "UTC"),
            ("-00:00", "+00:00", "UTC"),
        ],
    )
    def test_a_fixed_minute_precision_offset_is_accepted(
        self, requested: str, rendered: str, reported: str
    ) -> None:
        result = call("current_time", {"timezone": requested})
        assert isinstance(result.structured, dict)
        assert result.structured["timezone"] == reported
        # The instant is unchanged; only the wall-clock rendering differs.
        assert result.structured["unix_seconds"] == FIXED_UNIX_SECONDS
        assert str(result.structured["iso8601"]).endswith(rendered)

    def test_the_offset_shifts_the_rendered_wall_clock(self) -> None:
        result = call("current_time", {"timezone": "+05:45"})
        assert isinstance(result.structured, dict)
        assert result.structured["iso8601"] == "2026-09-17T20:34:03+05:45"

    @pytest.mark.parametrize(
        "offset",
        ["+14:01", "-14:01", "+15:00", "+00:60", "+5:45", "05:45", "+05", "UTC+5", "GMT", "", "Z"],
    )
    def test_an_out_of_range_or_malformed_offset_is_rejected(self, offset: str) -> None:
        assert failure("current_time", {"timezone": offset}).reason is (
            ToolFailureReason.ARGUMENTS_INVALID
        )

    def test_the_clock_is_injected_so_the_tool_is_deterministic_per_clock(self) -> None:
        later = datetime(2027, 1, 2, 3, 4, 5, tzinfo=UTC)
        executor = BuiltinToolExecutor(builtin_tool_specs(clock=lambda: later))
        first = asyncio.run(executor.execute(descriptor_for("current_time"), {}))
        second = asyncio.run(executor.execute(descriptor_for("current_time"), {}))
        assert first.structured == second.structured
        assert first.structured is not None and isinstance(first.structured, dict)
        assert first.structured["iso8601"] != "2026-09-17T14:49:03+00:00"

    def test_the_tool_is_not_argument_pure_and_does_not_claim_to_be(self) -> None:
        spec = spec_for("current_time")
        assert spec.risk_hints.idempotent is False

    def test_the_output_matches_the_declared_schema(self) -> None:
        output_schema = _output_schema("current_time")
        result = call("current_time", {"timezone": "-14:00"})
        assert validate_instance(output_schema, result.structured) is None


class TestCalculate:
    @pytest.mark.parametrize(
        "expression, expected",
        [
            ("1 + 2", "3"),
            ("2 * (3 + 4)", "14"),
            ("-5 + 2", "-3"),
            ("+7", "7"),
            ("10 / 4", "2.5"),
            ("1.50 + 0", "1.5"),
            ("1 / 3", "0.3333333333333333333333333333"),
            ("0.1 + 0.2", "0.3"),
            ("((1))", "1"),
            ("8 / 2 / 2", "2"),
            ("2 - 3 - 4", "-5"),
        ],
    )
    def test_exact_decimal_arithmetic(self, expression: str, expected: str) -> None:
        result = call("calculate", {"expression": expression})
        assert result.structured == {"result": expected}
        assert result.text == expected

    def test_negative_zero_is_canonicalised(self) -> None:
        for expression in ("-1 * 0", "0 / -1", "-0", "0 - 0"):
            assert call("calculate", {"expression": expression}).text == "0"

    def test_no_result_is_written_in_exponent_notation(self) -> None:
        for expression in ("1000000000000 * 1000", "1 / 10000", "0.000001 * 1"):
            text = call("calculate", {"expression": expression}).text
            assert "E" not in text.upper()

    def test_division_by_zero_is_a_typed_failure(self) -> None:
        assert failure("calculate", {"expression": "1 / 0"}).reason is (
            ToolFailureReason.ARGUMENTS_INVALID
        )

    @pytest.mark.parametrize(
        "expression",
        [
            "1 % 2",
            "1 ** 2",
            "2 ^ 3",
            "1e3",
            "abs(1)",
            "__import__('os')",
            "1; 2",
            "0x10",
            "\u0661 + 1",
            "1..2",
            ".5",
            "1.",
            "()",
            "1 +",
            "+",
            "1 2",
            "(1 + 2",
            "1 + 2)",
        ],
    )
    def test_unsupported_syntax_is_rejected_rather_than_evaluated(self, expression: str) -> None:
        assert failure("calculate", {"expression": expression}).reason is (
            ToolFailureReason.ARGUMENTS_INVALID
        )

    def test_an_empty_expression_is_rejected(self) -> None:
        assert failure("calculate", {"expression": "   "}).reason is (
            ToolFailureReason.ARGUMENTS_INVALID
        )

    def test_the_expression_length_bound_is_enforced(self) -> None:
        assert failure("calculate", {"expression": "1 + " * 200 + "1"}).reason is (
            ToolFailureReason.ARGUMENTS_INVALID
        )
        assert len("1" + " + 1" * 60) < MAX_EXPRESSION_LENGTH

    def test_the_token_bound_is_enforced(self) -> None:
        assert failure("calculate", {"expression": " + ".join(["1"] * 80)}).reason is (
            ToolFailureReason.ARGUMENTS_INVALID
        )

    def test_the_magnitude_bound_is_enforced(self) -> None:
        assert failure("calculate", {"expression": "9" * 16}).reason is (
            ToolFailureReason.ARGUMENTS_INVALID
        )
        assert failure("calculate", {"expression": "1000000000000000 * 10"}).reason is (
            ToolFailureReason.ARGUMENTS_INVALID
        )
        assert call("calculate", {"expression": "1000000000000000"}).text == "1000000000000000"

    def test_the_process_global_decimal_context_is_never_mutated(self) -> None:
        # A localcontext() only. Mutating the global would leak precision and rounding into every
        # other Decimal consumer in the process, and would make this tool unsafe to re-enter.
        context = decimal.getcontext()
        original = (context.prec, context.rounding)
        context.prec = 4
        try:
            assert call("calculate", {"expression": "1 / 3"}).text == (
                "0.3333333333333333333333333333"
            )
            assert decimal.getcontext().prec == 4
            assert (decimal.getcontext().rounding) == original[1]
        finally:
            context.prec = original[0]

    def test_a_result_is_text_rather_than_a_decimal_inside_structured_content(self) -> None:
        result = call("calculate", {"expression": "1 / 3"})
        assert isinstance(result.structured, dict)
        assert isinstance(result.structured["result"], str)

    def test_the_output_matches_the_declared_schema(self) -> None:
        output_schema = _output_schema("calculate")
        assert (
            validate_instance(output_schema, call("calculate", {"expression": "1+1"}).structured)
            is None
        )

    def test_rounding_is_half_even(self) -> None:
        # 1/7 rounded to 28 significant digits; half-even is what the frozen context specifies.
        assert call("calculate", {"expression": "1 / 7"}).text == ("0.1428571428571428571428571429")


DOCUMENT: dict[str, JsonValue] = {
    "profile": {"name": "Ada", "tags": ["x", "y"], "meta": {"deep": "value"}},
    "count": 3,
    "flag": True,
    "nothing": None,
}


class TestJsonTransform:
    def test_a_pointer_selects_a_nested_object(self) -> None:
        result = call("json_transform", {"document": DOCUMENT, "pointer": "/profile/meta"})
        assert result.structured == {"result": {"deep": "value"}}

    def test_an_empty_pointer_selects_the_whole_document(self) -> None:
        result = call("json_transform", {"document": DOCUMENT, "pointer": ""})
        assert result.structured == {"result": DOCUMENT}

    def test_array_traversal_uses_the_rfc6901_index_form(self) -> None:
        result = call(
            "json_transform", {"document": {"items": [{"a": 1}, {"b": 2}]}, "pointer": "/items/1"}
        )
        assert result.structured == {"result": {"b": 2}}

    def test_the_two_rfc6901_escapes_are_decoded(self) -> None:
        document: dict[str, JsonValue] = {"a/b": {"c~d": {"ok": 1}}}
        result = call("json_transform", {"document": document, "pointer": "/a~1b/c~0d"})
        assert result.structured == {"result": {"ok": 1}}

    def test_a_malformed_tilde_escape_is_rejected(self) -> None:
        for pointer in ("/a~2b", "/a~", "/~"):
            assert failure("json_transform", {"document": DOCUMENT, "pointer": pointer}).reason is (
                ToolFailureReason.ARGUMENTS_INVALID
            )

    @pytest.mark.parametrize("pointer", ["/missing", "/profile/nope", "/profile/tags/9", "profile"])
    def test_an_unresolvable_pointer_is_rejected(self, pointer: str) -> None:
        assert failure("json_transform", {"document": DOCUMENT, "pointer": pointer}).reason is (
            ToolFailureReason.ARGUMENTS_INVALID
        )

    @pytest.mark.parametrize("pointer", ["/profile/tags/01", "/profile/tags/-"])
    def test_a_malformed_array_index_is_rejected(self, pointer: str) -> None:
        assert failure("json_transform", {"document": DOCUMENT, "pointer": pointer}).reason is (
            ToolFailureReason.ARGUMENTS_INVALID
        )

    @pytest.mark.parametrize(
        "pointer", ["/count", "/flag", "/nothing", "/profile/name", "/profile/tags"]
    )
    def test_a_selection_that_is_not_an_object_is_rejected(self, pointer: str) -> None:
        # `count` is a number, `flag` a boolean, `nothing` null, `name` a string, `tags` an array.
        thrown = failure("json_transform", {"document": DOCUMENT, "pointer": pointer})
        assert thrown.reason is ToolFailureReason.ARGUMENTS_INVALID
        assert thrown.message == "Selected JSON value must be object."

    def test_a_non_object_selection_is_not_a_new_failure_reason(self) -> None:
        # The provider-neutral vocabulary stays small: this is ARGUMENTS_INVALID, not a per-tool
        # code.
        assert ToolFailureReason.ARGUMENTS_INVALID.value == "arguments_invalid"
        assert "result_not_an_object" not in {reason.value for reason in ToolFailureReason}

    def test_keys_project_a_subset_of_the_selected_object(self) -> None:
        result = call(
            "json_transform",
            {"document": DOCUMENT, "pointer": "/profile", "keys": ["name", "meta"]},
        )
        assert result.structured == {"result": {"name": "Ada", "meta": {"deep": "value"}}}

    def test_an_empty_keys_list_projects_nothing(self) -> None:
        result = call("json_transform", {"document": DOCUMENT, "pointer": "/profile", "keys": []})
        assert result.structured == {"result": {}}

    def test_an_absent_requested_key_is_an_error_not_a_silent_omission(self) -> None:
        thrown = failure(
            "json_transform",
            {"document": DOCUMENT, "pointer": "/profile", "keys": ["name", "nope"]},
        )
        assert thrown.reason is ToolFailureReason.ARGUMENTS_INVALID

    def test_duplicate_requested_keys_are_rejected(self) -> None:
        assert (
            failure(
                "json_transform",
                {"document": DOCUMENT, "pointer": "/profile", "keys": ["name", "name"]},
            ).reason
            is ToolFailureReason.ARGUMENTS_INVALID
        )

    def test_the_projection_bound_is_enforced(self) -> None:
        document: dict[str, JsonValue] = {"o": {f"k{index}": index for index in range(40)}}
        keys = [f"k{index}" for index in range(33)]
        arguments: dict[str, JsonValue] = {
            "document": document,
            "pointer": "/o",
            "keys": list(keys),
        }
        assert failure("json_transform", arguments).reason is ToolFailureReason.ARGUMENTS_INVALID

    def test_the_pointer_length_bound_is_enforced(self) -> None:
        assert (
            failure("json_transform", {"document": DOCUMENT, "pointer": "/" + "a" * 300}).reason
            is ToolFailureReason.ARGUMENTS_INVALID
        )

    def test_the_document_size_bound_is_enforced(self) -> None:
        oversized: dict[str, JsonValue] = {"big": "x" * 70000}
        assert (
            failure("json_transform", {"document": oversized, "pointer": ""}).reason
            is ToolFailureReason.RESULT_TOO_LARGE
        )

    def test_the_callers_document_is_never_mutated(self) -> None:
        import copy

        snapshot = copy.deepcopy(DOCUMENT)
        call("json_transform", {"document": DOCUMENT, "pointer": "/profile", "keys": ["name"]})
        call("json_transform", {"document": DOCUMENT, "pointer": "/profile/meta"})
        assert snapshot == DOCUMENT

    def test_the_output_matches_the_declared_schema(self) -> None:
        output_schema = _output_schema("json_transform")
        result = call("json_transform", {"document": DOCUMENT, "pointer": "/profile"})
        assert validate_instance(output_schema, result.structured) is None


class TestArgumentValidation:
    """The executor re-validates every argument against the tool's own schema before the handler."""

    @pytest.mark.parametrize(
        "tool, arguments",
        [
            ("current_time", {"timezone": 5}),
            ("current_time", {"unknown": "x"}),
            ("calculate", {"expression": 5}),
            ("calculate", {}),
            ("json_transform", {"document": DOCUMENT}),
            ("json_transform", {"document": [], "pointer": ""}),
            ("json_transform", {"document": DOCUMENT, "pointer": 1}),
            ("json_transform", {"document": DOCUMENT, "pointer": "", "keys": "not-a-list"}),
            ("json_transform", {"document": DOCUMENT, "pointer": "", "keys": [1]}),
        ],
    )
    def test_a_schema_violation_is_rejected_before_the_handler(
        self, tool: str, arguments: Mapping[str, JsonValue]
    ) -> None:
        assert failure(tool, arguments).reason is ToolFailureReason.ARGUMENTS_INVALID

    def test_an_unknown_builtin_is_an_internal_defect(self) -> None:
        thrown = failure("not_a_builtin", {})
        assert thrown.reason is ToolFailureReason.INTERNAL


class TestSideEffectFreeness:
    def test_no_builtin_touches_the_environment_or_filesystem(self) -> None:
        before = dict(os.environ)
        entries_before = set(os.listdir("."))
        call("current_time", {"timezone": "+05:45"})
        call("calculate", {"expression": "1/3"})
        call("json_transform", {"document": DOCUMENT, "pointer": "/profile"})
        assert dict(os.environ) == before
        assert set(os.listdir(".")) == entries_before

    def test_every_builtin_is_declared_read_only_and_closed_world(self) -> None:
        for spec in SPECS:
            assert spec.risk_hints.read_only is True
            assert spec.risk_hints.destructive is False
            assert spec.risk_hints.open_world is False

    def test_only_the_argument_pure_builtins_claim_idempotency(self) -> None:
        assert spec_for("calculate").risk_hints.idempotent is True
        assert spec_for("json_transform").risk_hints.idempotent is True
        # `current_time` returns a different instant by design, so it claims nothing.
        assert spec_for("current_time").risk_hints.idempotent is False

    def test_the_three_frozen_builtins_are_exactly_these(self) -> None:
        assert [spec.upstream_name for spec in SPECS] == [
            "current_time",
            "calculate",
            "json_transform",
        ]


def _schemas(upstream_name: str) -> tuple[CanonicalSchema, CanonicalSchema | None]:
    return canonical_spec_schemas(spec_for(upstream_name))


def _output_schema(upstream_name: str) -> CanonicalSchema:
    """The declared output schema. Every frozen built-in declares one."""
    _, output_schema = _schemas(upstream_name)
    assert output_schema is not None
    return output_schema


def test_the_clock_type_used_by_the_builtin_is_timezone_aware() -> None:
    assert FIXED_INSTANT.tzinfo is not None
    assert FIXED_INSTANT.astimezone(UTC) == FIXED_INSTANT


def test_specs_are_sequence_typed() -> None:
    assert isinstance(SPECS, Sequence)
