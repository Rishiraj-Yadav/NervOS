"""Stage E4 internal-event domain values: payload rules, the frozen envelope, and identity.

Everything here is pure. The canonical byte bound, the JSON contract, the envelope shape and the
identity grammar are all decided without a database, a transport or a clock, which is exactly why
they can be checked exhaustively -- and why the application suite below can treat them as settled.
"""

from __future__ import annotations

import hashlib

import pytest
from nervos_core.domain.events import (
    MAX_EVENT_PAYLOAD_CANONICAL_BYTES,
    InvalidEventPayload,
    compose_event_run_input,
    parse_event_payload,
    validate_event_id,
)
from nervos_core.domain.tools import JsonValue, canonical_json_text
from nervos_core.domain.triggers import InvalidTrigger


def accepted(value: object):
    """Parse a payload and fail loudly if it was refused."""
    return parse_event_payload(value)


# ------------------------------------------------------------------------------------------------
# The frozen bound
# ------------------------------------------------------------------------------------------------


def test_the_payload_bound_is_the_value_the_authorization_freezes() -> None:
    """The authorization fixes 65 536 canonical bytes. It is cited, not chosen here."""
    assert MAX_EVENT_PAYLOAD_CANONICAL_BYTES == 65_536


def test_a_json_object_is_accepted_with_canonical_metadata() -> None:
    """Canonical bytes -- not the raw object -- are what is measured, digested and counted."""
    value: JsonValue = {"b": [1, 2], "a": "x"}
    payload = accepted(value)

    assert payload.value == value
    assert payload.canonical_text == canonical_json_text(value)
    assert payload.digest == hashlib.sha256(payload.canonical_text.encode("utf-8")).digest()
    assert payload.byte_count == len(payload.canonical_text.encode("utf-8"))


def test_the_digest_measures_the_canonical_form_and_not_a_repr() -> None:
    """Equivalent JSON spellings share one digest after canonicalization."""
    first = accepted({"a": 1, "b": 2})
    second = accepted({"b": 2, "a": 1})

    assert first.canonical_text == second.canonical_text
    assert first.digest == second.digest
    assert first.byte_count == second.byte_count


def test_a_non_finite_number_is_refused() -> None:
    with pytest.raises(InvalidEventPayload):
        parse_event_payload({"reading": float("nan")})


def test_a_non_string_key_is_refused() -> None:
    with pytest.raises(InvalidEventPayload):
        parse_event_payload({1: "one"})  # type: ignore[dict-item]


def test_an_over_deep_structure_is_refused() -> None:
    value: object = "leaf"
    for _ in range(17):
        value = [value]
    with pytest.raises(InvalidEventPayload):
        parse_event_payload(value)


def test_a_payload_at_the_canonical_bound_is_accepted() -> None:
    """The bound is on canonical bytes, so padding that canonicalizes away does not count."""
    text = "x" * (MAX_EVENT_PAYLOAD_CANONICAL_BYTES - len('{"text":""}'))
    payload = accepted({"text": text})
    assert payload.byte_count == MAX_EVENT_PAYLOAD_CANONICAL_BYTES


def test_a_payload_one_byte_past_the_bound_is_refused() -> None:
    text = "x" * (MAX_EVENT_PAYLOAD_CANONICAL_BYTES - len('{"text":""}') + 1)
    with pytest.raises(InvalidEventPayload):
        parse_event_payload({"text": text})


# ------------------------------------------------------------------------------------------------
# The frozen Run-input envelope
# ------------------------------------------------------------------------------------------------


def test_the_run_input_envelope_carries_the_evental_labelled_field() -> None:
    payload = accepted({"temperature": 21})
    composed = compose_event_run_input("watch the sensor", "device.reading", payload.value)

    assert composed == canonical_json_text(
        {
            "instruction": "watch the sensor",
            "untrusted_event": {"type": "device.reading", "payload": {"temperature": 21}},
        }
    )


def test_the_envelope_shape_is_deterministic() -> None:
    first = compose_event_run_input("watch", "a.b", {"z": 1, "a": 2})
    second = compose_event_run_input("watch", "a.b", {"a": 2, "z": 1})
    assert first == second


# ------------------------------------------------------------------------------------------------
# The event identity grammar
# ------------------------------------------------------------------------------------------------


def test_a_bounded_non_empty_identity_is_accepted() -> None:
    assert validate_event_id("a" * 128) == "a" * 128


def test_an_empty_identity_is_refused() -> None:
    with pytest.raises(InvalidTrigger):
        validate_event_id("")


def test_an_overlong_identity_is_refused() -> None:
    with pytest.raises(InvalidTrigger):
        validate_event_id("a" * 129)


def test_a_nul_identity_is_refused() -> None:
    with pytest.raises(InvalidTrigger):
        validate_event_id("evt\x00id")
