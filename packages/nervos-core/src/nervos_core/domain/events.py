"""Stage E4 internal events: the provider-neutral event envelope.

An internal event is **data, and only data**. This module carries no transport, no HTTP, no
database type and no scheduler: it decides only what an internally-published event *is*, and
whether one is representable at all.

Three facts the authorization freezes are implemented here and nowhere else:

* the payload is an existing **JsonValue**, validated and canonicalized through the Stage-D JSON
  contract, never re-parsed by a second parser;
* the canonical payload bound is **65 536 bytes** measured over
  :func:`~nervos_core.domain.tools.canonical_json_text` of the payload -- this module adds no
  ingestion surface and no streaming parser;
* the occurrence metadata is the SHA-256 digest of those canonical UTF-8 bytes and their exact
  byte count. It is a digest of the **canonical** form, never of a Python ``repr`` and never of a
  non-canonical serialization.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from nervos_core.domain.tools import (
    JSON_VALUE_MAX_DEPTH,
    JsonValue,
    canonical_json_text,
    validate_json_value,
)
from nervos_core.domain.triggers import (
    MAX_EVENT_ID_LENGTH,
    InvalidTrigger,
)

#: The frozen canonical-payload bound: the maximum UTF-8 byte length of the canonical JSON text
#: of one event payload. Measured over the canonical form, so an over-deep, over-wide or
#: over-serialized body is refused before it can be stored or sent onward.
MAX_EVENT_PAYLOAD_CANONICAL_BYTES = 65_536


class InvalidEventPayload(ValueError):
    """The payload is outside the frozen event contract: not JSON, too deep, or too large."""


@dataclass(frozen=True, slots=True)
class EventPayload:
    """One validated, canonicalized internal-event payload.

    ``value`` is the original JSON value; ``canonical_text`` is the single serialized form every
    downstream consumer agrees on; ``digest`` is the SHA-256 of the canonical UTF-8 bytes; and
    ``byte_count`` is the exact byte length of that canonical form. The digest is excluded from
    ``repr`` for the same reason a credential digest is: it is derived, not meaningful to read.
    """

    value: JsonValue
    canonical_text: str
    digest: bytes = field(repr=False)
    byte_count: int


def parse_event_payload(value: object) -> EventPayload:
    """Validate one payload and derive its canonical metadata, or refuse it.

    Validation runs first and canonicalization second, so nothing unbounded is ever serialized:
    a value outside the JSON contract -- a non-finite number, a non-string key, an over-deep
    structure -- is rejected *before* canonical bytes exist. The canonical byte count is the one
    bound measured, and it is measured on the canonical form rather than the raw object.
    """
    rejection = validate_json_value(value, max_depth=JSON_VALUE_MAX_DEPTH)
    if rejection is not None:
        raise InvalidEventPayload(f"event payload rejected: {rejection.reason} at {rejection.path}")
    canonical = canonical_json_text(value)  # type: ignore[arg-type]
    encoded = canonical.encode("utf-8")
    if len(encoded) > MAX_EVENT_PAYLOAD_CANONICAL_BYTES:
        raise InvalidEventPayload(
            f"event payload exceeds {MAX_EVENT_PAYLOAD_CANONICAL_BYTES} canonical bytes"
        )
    return EventPayload(
        value=value,  # type: ignore[arg-type]
        canonical_text=canonical,
        digest=hashlib.sha256(encoded).digest(),
        byte_count=len(encoded),
    )


def compose_event_run_input(instruction: str, event_type: str, payload: JsonValue) -> str:
    """The one composition of an operator's instruction and an event, in the frozen envelope.

    The result is an ordinary ``Run.input_text``. The operator's instruction is the only
    human-authored part; the event is the untrusted half, kept inside its own labelled field so it
    can never be mistaken for the instruction. ``type`` and ``payload`` are both event data.
    """
    envelope: dict[str, JsonValue] = {
        "instruction": instruction,
        "untrusted_event": {"type": event_type, "payload": payload},
    }
    return canonical_json_text(envelope)


def validate_event_id(value: str) -> str:
    """Validate an event identity in the frozen shape, the same rule the occurrence column uses.

    It is a bounded, non-empty, NUL-free string -- deliberately the *stored* grammar and nothing
    narrower, so a valid identity can never fail at write time as a persistence shape error.
    """
    if not value or len(value) > MAX_EVENT_ID_LENGTH or "\x00" in value:
        raise InvalidTrigger("invalid event id")
    return value


__all__ = [
    "MAX_EVENT_PAYLOAD_CANONICAL_BYTES",
    "EventPayload",
    "InvalidEventPayload",
    "compose_event_run_input",
    "parse_event_payload",
    "validate_event_id",
]
