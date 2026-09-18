"""D4 audit metadata: the result envelope, argument shape, and call-id contract.

These are the values that survive a tool call as evidence rather than as content, so the tests focus
on the properties that make evidence trustworthy: one canonical serialization, a digest and a byte
count that describe the same object, and a shape that is never shortened into something else.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from nervos_core.application.tool_invocations import (
    ALLOWED_DECISION,
    MAX_ARGUMENT_SHAPE_BYTES,
    MAX_ARGUMENT_SHAPE_KEY_LENGTH,
    MAX_ARGUMENT_SHAPE_KEYS,
    arguments_digest,
    arguments_shape,
    canonical_envelope_bytes,
    parse_arguments,
    permission_decision_value,
    result_envelope,
)
from nervos_core.application.tool_loop import (
    MAX_PROVIDER_CALL_ID_BYTES,
    STRUCTURED_OMITTED_NOTE,
    TEXT_TRUNCATED_NOTE,
    bounded_observation,
    is_valid_provider_call_id,
    merge_usage,
)
from nervos_core.application.tool_permissions import (
    PermissionDecision,
    PermissionDenialReason,
)
from nervos_core.domain.runs import ModelUsage
from nervos_core.domain.tools import JsonValue


class TestResultEnvelope:
    def test_digest_and_bytes_describe_the_same_object(self) -> None:
        payload = canonical_envelope_bytes(text="hello", structured={"b": 2, "a": 1})
        envelope = result_envelope(text="hello", structured={"b": 2, "a": 1})

        assert envelope.digest == hashlib.sha256(payload).hexdigest()
        assert envelope.bytes == len(payload)

    def test_the_envelope_is_canonical_regardless_of_key_order(self) -> None:
        left = result_envelope(text="t", structured={"a": 1, "b": [1, 2]})
        right = result_envelope(text="t", structured={"b": [1, 2], "a": 1})

        assert left == right

    def test_an_absent_structured_value_differs_from_an_empty_one(self) -> None:
        absent = result_envelope(text="t", structured=None)
        empty = result_envelope(text="t", structured={})

        assert absent.digest != empty.digest

    def test_the_evidence_covers_the_untruncated_result_not_the_observation(self) -> None:
        value: JsonValue = {"payload": "x" * 5000}
        envelope = result_envelope(text="short", structured=value)
        observation = bounded_observation(
            text="short", structured=value, is_error=False, max_bytes=1024
        )

        assert observation.truncated is True
        assert envelope.bytes > 5000


class TestBoundedObservation:
    def test_a_small_result_is_passed_through_untouched(self) -> None:
        observation = bounded_observation(
            text="small", structured={"a": 1}, is_error=False, max_bytes=1024
        )

        assert observation.text == "small"
        assert observation.structured == {"a": 1}
        assert observation.truncated is False

    def test_an_oversized_structured_value_is_dropped_whole_and_disclosed(self) -> None:
        observation = bounded_observation(
            text="readable",
            structured={"payload": "x" * 4000},
            is_error=False,
            max_bytes=1024,
        )

        assert observation.structured is None
        assert observation.text == f"readable{STRUCTURED_OMITTED_NOTE}"
        assert observation.truncated is True

    def test_oversized_text_is_head_truncated_and_disclosed_within_the_limit(self) -> None:
        observation = bounded_observation(
            text="a" * 5000, structured=None, is_error=False, max_bytes=1024
        )

        assert observation.truncated is True
        assert observation.structured is None
        assert observation.text.endswith(TEXT_TRUNCATED_NOTE)
        assert len(observation.text.encode("utf-8")) <= 1024

    def test_truncation_never_splits_a_multibyte_code_point(self) -> None:
        # Three-byte characters, so a naive byte slice would end mid-character.
        observation = bounded_observation(
            text="€" * 2000, structured=None, is_error=False, max_bytes=1000
        )

        assert "�" not in observation.text
        assert observation.text.encode("utf-8").decode("utf-8") == observation.text

    def test_the_final_payload_including_its_disclosure_fits_the_limit(self) -> None:
        for limit in (1024, 2048, 65_536):
            observation = bounded_observation(
                text="z" * 200_000,
                structured={"k": "v" * 100_000},
                is_error=False,
                max_bytes=limit,
            )
            measured = len(observation.text.encode("utf-8"))
            if observation.structured is not None:
                measured += 1 + len(
                    json.dumps(observation.structured, sort_keys=True, separators=(",", ":"))
                )
            assert measured <= limit, limit

    def test_an_error_observation_keeps_its_flag(self) -> None:
        observation = bounded_observation(
            text="failed safely", structured=None, is_error=True, max_bytes=1024
        )

        assert observation.is_error is True


class TestArgumentsMetadata:
    def test_digest_covers_the_full_arguments_not_the_shape(self) -> None:
        left = arguments_digest({"a": "one"})
        right = arguments_digest({"a": "two"})

        assert left != right
        assert arguments_shape({"a": "one"}) == arguments_shape({"a": "two"})

    def test_the_shape_is_sorted_exact_keys_with_no_values(self) -> None:
        shape = arguments_shape({"z": 1, "a": {"nested": "secret"}})

        assert shape == '["a","z"]'
        assert "secret" not in shape

    def test_too_many_keys_yield_no_shape_rather_than_a_partial_one(self) -> None:
        arguments = {f"k{index}": index for index in range(MAX_ARGUMENT_SHAPE_KEYS + 1)}

        assert arguments_shape(arguments) is None

    def test_an_over_long_key_yields_no_shape_rather_than_a_truncated_one(self) -> None:
        long_key = "k" * (MAX_ARGUMENT_SHAPE_KEY_LENGTH + 1)

        assert arguments_shape({long_key: 1}) is None

    def test_distinct_long_keys_can_never_collide_into_one_shape(self) -> None:
        left = {"k" * (MAX_ARGUMENT_SHAPE_KEY_LENGTH + 1) + "a": 1}
        right = {"k" * (MAX_ARGUMENT_SHAPE_KEY_LENGTH + 1) + "b": 1}

        assert arguments_shape(left) is None
        assert arguments_shape(right) is None
        assert arguments_digest(left) != arguments_digest(right)

    def test_an_encoded_shape_over_the_byte_bound_yields_no_shape(self) -> None:
        # Every key within the per-key bound, but enough of them that the encoded skeleton -- which
        # also carries quotes, commas and brackets -- exceeds the durable 4096-byte bound.
        keys = {"k" * 126 + f"{index:02d}": index for index in range(MAX_ARGUMENT_SHAPE_KEYS)}
        assert all(len(key) <= MAX_ARGUMENT_SHAPE_KEY_LENGTH for key in keys)
        encoded = json.dumps(sorted(keys), separators=(",", ":"))
        assert len(encoded.encode("utf-8")) > MAX_ARGUMENT_SHAPE_BYTES

        assert arguments_shape(keys) is None


class TestParseArguments:
    def test_a_json_object_parses(self) -> None:
        assert parse_arguments('{"a":1}') == {"a": 1}

    @pytest.mark.parametrize("payload", ["not json", "[1,2]", '"text"', "5", "null", "{}]"])
    def test_anything_that_is_not_a_json_object_is_refused(self, payload: str) -> None:
        assert parse_arguments(payload) is None


class TestProviderCallIds:
    def test_a_printable_identifier_is_accepted(self) -> None:
        assert is_valid_provider_call_id("call_abc-123") is True

    @pytest.mark.parametrize("value", ["", "has space\n", "tab\there", "é", "x" * 129])
    def test_blank_non_printable_or_oversized_identifiers_are_refused(self, value: str) -> None:
        assert is_valid_provider_call_id(value) is False

    def test_the_bound_is_measured_in_bytes(self) -> None:
        assert is_valid_provider_call_id("x" * MAX_PROVIDER_CALL_ID_BYTES) is True


class TestPermissionDecisionRendering:
    def test_an_allow_renders_the_frozen_allowed_value(self) -> None:
        assert permission_decision_value(PermissionDecision(allowed=True)) == ALLOWED_DECISION

    @pytest.mark.parametrize(
        ("reason", "expected"),
        [
            (PermissionDenialReason.NOT_GRANTED, "denied_not_granted"),
            (PermissionDenialReason.GRANT_AFTER_RUN_CUTOFF, "denied_grant_after_run_cutoff"),
            (PermissionDenialReason.DEFINITION_CHANGED, "denied_definition_changed"),
            (PermissionDenialReason.DEFINITION_UNAVAILABLE, "denied_definition_unavailable"),
            (PermissionDenialReason.CONNECTION_DISABLED, "denied_connection_disabled"),
            (PermissionDenialReason.OWNER_MISMATCH, "denied_owner_mismatch"),
            (PermissionDenialReason.HARD_POLICY_DENIED, "denied_hard_policy_denied"),
        ],
    )
    def test_every_denial_renders_a_bounded_durable_value(
        self, reason: PermissionDenialReason, expected: str
    ) -> None:
        rendered = permission_decision_value(PermissionDecision(allowed=False, reason=reason))

        assert rendered == expected
        # The durable column is bounded at 1..64 and must match D1's `denied_*` shape.
        assert rendered.startswith("denied_")
        assert 1 <= len(rendered) <= 64


class TestUsageAggregation:
    def test_reported_fields_are_summed_independently(self) -> None:
        total = merge_usage(ModelUsage(1, 2, None), ModelUsage(10, None, 5))

        assert total == ModelUsage(11, 2, 5)

    def test_a_field_stays_absent_only_when_no_turn_reported_it(self) -> None:
        assert merge_usage(ModelUsage(None, 3, None), None) == ModelUsage(None, 3, None)
        assert merge_usage(ModelUsage(), ModelUsage()) == ModelUsage()

    def test_nothing_is_estimated_from_a_partial_report(self) -> None:
        # A turn reporting only input tokens must not cause a total to be invented.
        total = merge_usage(ModelUsage(), ModelUsage(input_tokens=7))

        assert total == ModelUsage(input_tokens=7, output_tokens=None, total_tokens=None)
