"""The rules of a webhook delivery: bounded JSON, canonical rendering, and one composition.

Nothing here performs I/O, names a transport, or knows what a database is. The module is the
ingress's *semantics*; `application/webhooks.py` orchestrates it and `nervos_api/hooks/` carries it
over HTTP.

Three facts ADR 0019 freezes are implemented here and nowhere else:

* the body bound is **65 536 bytes**, refused before parsing (ADR 0019:145,224);
* the body is **a single JSON object** (ADR 0019:144), and the payload is **data, and only data**
  (ADR 0019:154);
* the model-visible input is the operator's own instruction **plus** a delimited, canonical
  rendering of the payload (ADR 0019:156-176), and the composed result must satisfy the Run's own
  input bounds or the occurrence is skipped and no Run is created (ADR 0018:576).

The canonical JSON form is not invented here: `canonical_json_text` and `validate_json_value` are
Stage D's existing contract (`domain/tools.py`), reused so the ingress and the tool layer cannot
disagree about what a canonical JSON value is.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from nervos_core.domain.tools import (
    JsonValue,
    JsonValueReason,
    canonical_json_text,
    validate_json_value,
)
from nervos_core.domain.triggers import (
    MAX_IDEMPOTENCY_KEY_LENGTH,
    InvalidTrigger,
    validate_webhook_public_id,
)

#: ADR 0019:224 -- "Body | 65 536 bytes, refused before parsing". The bound is on the **raw request
#: bytes**, not on the canonical form, and it is applied before any JSON work and before any
#: database work.
WEBHOOK_BODY_MAX_BYTES = 65_536

#: The ingress's `Idempotency-Key` grammar: visible ASCII, 1..128 characters. It is deliberately a
#: strict **subset** of what a `trigger_occurrences.idempotency_key` column will store (E1's bound
#: is 1..128 characters with no NUL, `MAX_IDEMPOTENCY_KEY_LENGTH`), so the implication
#: "the ingress accepted this key => the database will accept it" holds by construction and a
#: well-formed request can never fail as a generic persistence error.
_IDEMPOTENCY_KEY_PATTERN = re.compile(rf"[!#-~]{{1,{MAX_IDEMPOTENCY_KEY_LENGTH}}}\Z")

#: The two field names of the frozen envelope. `_PAYLOAD_KEY` names the boundary in the key itself:
#: it is the label ADR 0019:169 requires, and it is structural rather than a delimiter a payload
#: could imitate.
_INSTRUCTION_KEY = "instruction"
_PAYLOAD_KEY = "untrusted_webhook_payload"

#: The public skip vocabulary. An occurrence's `skip_code` is a frozen `SkipReason` value; only
#: these two are reachable on the webhook path, and only these are ever published (ADR 0019:209).
PUBLIC_WEBHOOK_SKIP_CODES = frozenset({"agent_disabled", "input_too_large"})


class WebhookPayloadRejection(StrEnum):
    """Why a delivered body is not an acceptable webhook payload.

    A verdict rather than an exception, following the same convention
    :mod:`nervos_core.application.tool_schema` uses: classifying arbitrary bytes is this module's
    whole job, so an unacceptable body is a *returned answer*, never a raised error a caller could
    forget to translate. Every member maps to one static public code; none of them is ever
    interpolated into a response or a log.
    """

    EMPTY_BODY = "empty_body"
    NOT_UTF8 = "not_utf8"
    MALFORMED = "malformed"
    NOT_AN_OBJECT = "not_an_object"
    DUPLICATE_KEY = "duplicate_key"
    NOT_A_JSON_VALUE = "not_a_json_value"
    NON_FINITE_NUMBER = "non_finite_number"
    DEPTH_EXCEEDED = "depth_exceeded"


@dataclass(frozen=True, slots=True)
class WebhookPayload:
    """A validated delivery body, canonicalised and measured.

    ``value`` is the parsed JSON object; ``canonical_text`` is its one deterministic rendering;
    ``digest`` and ``byte_count`` describe the **raw** bytes that were received, which is what the
    occurrence records. The three are computed together so no caller can pair a canonical form with
    the wrong raw measurement.
    """

    value: JsonValue
    canonical_text: str
    digest: bytes
    byte_count: int


class _DuplicateKey(ValueError):
    """Internal parser signal. It never escapes this module."""


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build one object, refusing a repeated key rather than silently keeping the last one.

    Python's default behaviour keeps the last duplicate, which would silently transform the bytes a
    sender actually wrote into a different model-visible block. Refusing is deterministic and
    honest: the body was not the well-formed object it claimed to be. No security property depends
    on this (the persisted digest is over the original bytes), which is exactly why it is a stated
    decision rather than a load-bearing check.
    """
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise _DuplicateKey(key)
        seen.add(key)
    return dict(pairs)


def parse_webhook_payload(raw: bytes) -> WebhookPayload | WebhookPayloadRejection:
    """Validate, canonicalise and measure one delivered body. Total: never raises.

    The order is deliberate. Cheap structural refusals (empty, not UTF-8) come first; the parse
    comes next and is where duplicate keys, malformed syntax and pathological nesting are caught;
    only then is the value classified against Stage D's JSON contract, which is what rejects
    non-finite numbers and over-deep structures. Nothing in this function is reachable before
    authentication succeeds.
    """
    if not raw:
        return WebhookPayloadRejection.EMPTY_BODY
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return WebhookPayloadRejection.NOT_UTF8
    try:
        value = json.loads(text, object_pairs_hook=_object_without_duplicate_keys)
    except _DuplicateKey:
        return WebhookPayloadRejection.DUPLICATE_KEY
    except RecursionError:
        # `json.loads` is recursive, so a deeply nested body fails here rather than in the depth
        # check below. Mapping it to the ordinary malformed verdict is what keeps it a 400 instead
        # of an unhandled 500.
        return WebhookPayloadRejection.MALFORMED
    except ValueError:
        # `json.JSONDecodeError` is a `ValueError`. The parser's own message is never kept, because
        # it would reach a public response.
        return WebhookPayloadRejection.MALFORMED
    if not isinstance(value, dict):
        return WebhookPayloadRejection.NOT_AN_OBJECT
    # `json.loads` is typed `Any`, so the shape is asserted once here rather than carried as an
    # unknown through the contract checks below.
    mapping = cast("dict[str, object]", value)
    rejection = validate_json_value(mapping)
    if rejection is not None:
        if rejection.reason is JsonValueReason.NON_FINITE_NUMBER:
            return WebhookPayloadRejection.NON_FINITE_NUMBER
        if rejection.reason is JsonValueReason.DEPTH_EXCEEDED:
            return WebhookPayloadRejection.DEPTH_EXCEEDED
        return WebhookPayloadRejection.NOT_A_JSON_VALUE
    payload = cast("JsonValue", mapping)
    return WebhookPayload(
        value=payload,
        canonical_text=canonical_json_text(payload),
        digest=hashlib.sha256(raw).digest(),
        byte_count=len(raw),
    )


def compose_run_input(instruction: str, payload: WebhookPayload) -> str:
    """The one composition of an operator's instruction and an untrusted payload.

    The result is an ordinary `Run.input_text`. There is no second chat or request schema, and
    nothing here touches a system instruction: the operator's text is the only human-authored part,
    it is always read from the durable trigger row, and the payload cannot leave its own field
    because JSON escaping makes a break-out unrepresentable rather than merely unlikely.
    """
    envelope: dict[str, JsonValue] = {_INSTRUCTION_KEY: instruction, _PAYLOAD_KEY: payload.value}
    return canonical_json_text(envelope)


def is_well_formed_public_id(value: str) -> bool:
    """Whether a locator has the frozen shape. E1's validator remains the only authority."""
    try:
        validate_webhook_public_id(value)
    except InvalidTrigger:
        return False
    return True


def is_well_formed_idempotency_key(value: str) -> bool:
    """Whether a caller-supplied key is inside the ingress grammar."""
    return _IDEMPOTENCY_KEY_PATTERN.fullmatch(value) is not None


__all__ = [
    "PUBLIC_WEBHOOK_SKIP_CODES",
    "WEBHOOK_BODY_MAX_BYTES",
    "WebhookPayload",
    "WebhookPayloadRejection",
    "compose_run_input",
    "is_well_formed_idempotency_key",
    "is_well_formed_public_id",
    "parse_webhook_payload",
]
