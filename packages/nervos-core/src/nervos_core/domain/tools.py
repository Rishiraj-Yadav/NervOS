"""Provider-neutral tool domain values: identity, naming, and fingerprint.

This is the domain half of Stage D's tool layer. It holds pure values only -- no persistence, no
transport, no provider, and no model. Three things live here and nowhere else:

* the JSON value contract (:data:`JsonValue`) that every tool input and output must satisfy;
* the durable identity of a tool definition, and the model-facing name derived from it;
* the fingerprint over everything a model or a user materially reviewed.

**Two ADR 0015 clarifications are implemented here**, both recorded as errata at D3 finalization:

1. :func:`escape_upstream_name` escapes over the UTF-8 **bytes** of the name, not over its code
   points. A fixed two-hex-digit body cannot represent a code point above ``U+00FF``, so the
   published rule is not injective for non-ASCII names: ``'\\u20ac'`` would escape to ``_20ac``,
   exactly as ``' ' + 'ac'`` does. Escaping bytes keeps the two-digit body total and the escape
   unambiguous, so distinct upstream names cannot collide.
2. :func:`model_tool_name` appends the suffix unconditionally, built-ins included, so there is one
   code path and the length budget holds for both namespaces. The human-readable names written in
   ADR 0015 are labels for humans, **not** persisted ``model_name`` values.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

# The JSON value contract. A tool input, a tool result and a canonical schema are all built from
# exactly these types and nothing else -- there is no third "arbitrary Python object" case, and no
# code in Stage D may treat `object` as though there were.
type JsonScalar = bool | int | float | str | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]

# Structural safety bound shared by the JSON contract and by canonical instance validation, so the
# two cannot drift into disagreeing about how deep a tool value may be.
JSON_VALUE_MAX_DEPTH = 16

MAX_UPSTREAM_NAME_LENGTH = 128
MAX_MODEL_NAME_LENGTH = 64
MIN_MODEL_NAME_BUDGET = 8
MAX_DISPLAY_NAME_LENGTH = 100
MAX_DISPLAY_NAME_BYTES = 400
MAX_DESCRIPTION_BYTES = 65536
MODEL_NAME_SUFFIX_HEX_LENGTH = 12

_BUILTIN_PREFIX = "nervos__builtin__"
_MCP_PREFIX = "nervos__c"
_NAMESPACE_SEPARATOR = "__"
_ESCAPE_PREFIX = "_"
_HYPHEN = 0x2D
_DIGIT_ZERO = 0x30
_DIGIT_NINE = 0x39
_LOWER_A = 0x61
_LOWER_Z = 0x7A

_MODEL_NAME_ALPHABET = frozenset(
    [chr(code) for code in range(_LOWER_A, _LOWER_Z + 1)]
    + [chr(code) for code in range(_DIGIT_ZERO, _DIGIT_NINE + 1)]
    + ["-", "_"]
)
_MODEL_NAME_LEADING = frozenset(
    [chr(code) for code in range(_LOWER_A, _LOWER_Z + 1)]
    + [chr(code) for code in range(_DIGIT_ZERO, _DIGIT_NINE + 1)]
)


class InvalidToolDefinition(ValueError):
    """Raised when a tool definition value violates a domain invariant."""


class InvalidToolName(ValueError):
    """Raised when a model-facing name cannot be generated within its bounds."""


class ToolSourceKind(StrEnum):
    """Where a tool definition comes from."""

    BUILTIN = "builtin"
    MCP = "mcp"


class DefinitionStatus(StrEnum):
    """The durable availability of a tool definition.

    ``unsupported_schema`` is meaningful for externally discovered tools: the definition is listed
    so the user can see the precise limitation, and it is never offered to a model. A NervOS-owned
    built-in never reaches this status -- such a schema is a defect in our own code and registration
    fails loudly instead.
    """

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED_SCHEMA = "unsupported_schema"


class JsonValueReason(StrEnum):
    """Typed reasons a value is not part of the JSON contract."""

    NOT_A_JSON_TYPE = "not_a_json_type"
    NON_FINITE_NUMBER = "non_finite_number"
    NON_STRING_KEY = "non_string_key"
    DEPTH_EXCEEDED = "depth_exceeded"


@dataclass(frozen=True, slots=True)
class JsonRejection:
    """One typed reason a value failed the JSON contract, with its location."""

    reason: JsonValueReason
    path: str


@dataclass(frozen=True, slots=True)
class ToolSourceRef:
    """The durable identity of one tool source.

    A built-in source is ``(BUILTIN, None)`` and is unique. An MCP source is ``(MCP, <connection
    id>)``, so several connections of the same kind coexist without a redesign. The invariant is
    enforced at construction rather than checked by callers.
    """

    source_kind: ToolSourceKind
    source_id: int | None

    def __post_init__(self) -> None:
        if self.source_kind is ToolSourceKind.BUILTIN:
            if self.source_id is not None:
                raise InvalidToolDefinition("a builtin source has no source id")
        elif self.source_id is None or self.source_id <= 0:
            raise InvalidToolDefinition("an mcp source requires a positive source id")

    @property
    def is_builtin(self) -> bool:
        return self.source_kind is ToolSourceKind.BUILTIN


@dataclass(frozen=True, slots=True)
class RiskHints:
    """The source's own annotation claims about a tool.

    **Untrusted hints and presentation metadata only.** A server can lie, so these may never grant
    authority, remove authority, permit automatic retry, or satisfy any predicate in a permission
    decision or a replay guard. The defaults are the conservative reading the MCP specification
    itself prescribes, so an unannotated tool is treated as possibly destructive and possibly
    open-world.
    """

    read_only: bool = False
    destructive: bool = True
    idempotent: bool = False
    open_world: bool = True


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    """A **durable** tool identity.

    There is no unpersisted form of this type: ``tool_definition_id`` is a real row id, so a
    descriptor without durable identity cannot be constructed and no consumer ever has to assert
    that the id is present. The pre-persistence shape is a separate, module-local structure.
    """

    tool_definition_id: int
    upstream_name: str
    model_name: str
    source_kind: ToolSourceKind
    source_id: int | None
    display_name: str
    description: str
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue] | None
    risk_hints: RiskHints
    fingerprint: str

    def __post_init__(self) -> None:
        if self.tool_definition_id <= 0:
            raise InvalidToolDefinition("a tool definition id must be positive")
        _require_upstream_name(self.upstream_name)
        _require_model_name(self.model_name)
        if not 1 <= len(self.display_name) <= MAX_DISPLAY_NAME_LENGTH:
            raise InvalidToolDefinition("display name length out of bounds")
        if self.display_name != self.display_name.strip():
            raise InvalidToolDefinition("display name must be trimmed")
        if len(self.display_name.encode("utf-8")) > MAX_DISPLAY_NAME_BYTES:
            raise InvalidToolDefinition("display name bytes out of bounds")
        if len(self.description.encode("utf-8")) > MAX_DESCRIPTION_BYTES:
            raise InvalidToolDefinition("description bytes out of bounds")
        if len(self.fingerprint) != 64 or not _is_lower_hex(self.fingerprint):
            raise InvalidToolDefinition("fingerprint must be a 64-character lowercase sha256 hex")

    @property
    def source_ref(self) -> ToolSourceRef:
        return ToolSourceRef(self.source_kind, self.source_id)


def escape_upstream_name(value: str) -> str:
    """Escape one upstream name into the model-facing alphabet.

    Lowercase first, then escape each **UTF-8 byte**: the permitted bytes pass through, and every
    other byte becomes ``_`` followed by exactly two lowercase hex digits. Because a literal ``_``
    is itself escaped (to ``_5f``), ``_`` always begins exactly one two-digit escape, so the
    encoding is unambiguous -- and therefore injective, so distinct upstream names cannot collide.
    """
    escaped: list[str] = []
    for byte in value.lower().encode("utf-8"):
        if _LOWER_A <= byte <= _LOWER_Z or _DIGIT_ZERO <= byte <= _DIGIT_NINE or byte == _HYPHEN:
            escaped.append(chr(byte))
        else:
            escaped.append(f"{_ESCAPE_PREFIX}{byte:02x}")
    return "".join(escaped)


def model_tool_name(source_ref: ToolSourceRef, upstream_name: str) -> str:
    """Derive the durable model-facing name for one tool of one source.

    The fixed parts and the hash suffix are reserved **before** the variable segment is truncated,
    so the result is bounded by construction rather than by convention. The suffix is hashed over
    the **untruncated** composed name, so it disambiguates names that truncate to the same prefix.
    """
    fixed = _fixed_name_prefix(source_ref)
    escaped = escape_upstream_name(upstream_name)
    suffix = f"{_ESCAPE_PREFIX}{_sha256_hex(fixed + escaped)[:MODEL_NAME_SUFFIX_HEX_LENGTH]}"
    budget = MAX_MODEL_NAME_LENGTH - len(fixed) - len(suffix)
    if budget < MIN_MODEL_NAME_BUDGET:
        raise InvalidToolName(
            f"no room for a model-facing name: {len(fixed)} fixed + {len(suffix)} suffix"
        )
    return f"{fixed}{escaped[:budget]}{suffix}"


def definition_fingerprint(
    *,
    model_name: str,
    upstream_name: str,
    description: str,
    input_schema: Mapping[str, JsonValue],
    output_schema: Mapping[str, JsonValue] | None,
    source_kind: ToolSourceKind,
    source_id: int | None,
    risk_hints: RiskHints,
) -> str:
    """Fingerprint everything a model or a user materially reviewed.

    The inputs are already-parsed canonical structures, so two byte-different spellings of the same
    logical schema fingerprint identically and a cosmetic re-format cannot suspend a grant. Cache
    expiry, discovery and transport-health timestamps are deliberately excluded, so ordinary server
    housekeeping does not churn grants either.

    Including the risk hints is what makes a source changing the risk metadata a user reviewed
    suspend the grant. It does **not** make them trusted: they remain presentation-only.
    """
    payload: dict[str, JsonValue] = {
        "description": description,
        "input_schema": dict(input_schema),
        "model_name": model_name,
        "output_schema": None if output_schema is None else dict(output_schema),
        "risk_hints": {
            "destructive": risk_hints.destructive,
            "idempotent": risk_hints.idempotent,
            "open_world": risk_hints.open_world,
            "read_only": risk_hints.read_only,
        },
        "source_id": source_id,
        "source_kind": str(source_kind),
        "upstream_name": upstream_name,
    }
    return _sha256_hex(canonical_json_text(payload))


def canonical_json_text(value: JsonValue) -> str:
    """Serialise a JSON value deterministically: sorted keys, no incidental whitespace, UTF-8.

    The caller must have already established that the value is part of the JSON contract; this
    function is the canonical form used by the fingerprint and by byte-size measurement.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def validate_json_value(
    value: object,
    *,
    path: str = "$",
    depth: int = 0,
    max_depth: int = JSON_VALUE_MAX_DEPTH,
) -> JsonRejection | None:
    """Return the typed reason a value is outside the JSON contract, or ``None`` if it is inside it.

    ``value`` is typed ``object`` because classifying arbitrary input is this function's whole job;
    the *output* contract is :data:`JsonValue`. No coercion happens and nothing is dropped:
    ``Decimal``, ``bytes``, ``datetime``, ``set``, ``tuple``, a custom class, a non-``dict`` mapping
    and a non-finite float are each rejected rather than converted.
    """
    if value is None or isinstance(value, bool | int | str):
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return JsonRejection(JsonValueReason.NON_FINITE_NUMBER, path)
        return None
    if isinstance(value, list):
        if depth >= max_depth:
            return JsonRejection(JsonValueReason.DEPTH_EXCEEDED, path)
        for index, item in enumerate(cast("list[object]", value)):
            rejection = validate_json_value(
                item, path=f"{path}[{index}]", depth=depth + 1, max_depth=max_depth
            )
            if rejection is not None:
                return rejection
        return None
    if isinstance(value, dict):
        if depth >= max_depth:
            return JsonRejection(JsonValueReason.DEPTH_EXCEEDED, path)
        for key, item in cast("dict[object, object]", value).items():
            if not isinstance(key, str):
                return JsonRejection(JsonValueReason.NON_STRING_KEY, path)
            rejection = validate_json_value(
                item, path=f"{path}.{key}", depth=depth + 1, max_depth=max_depth
            )
            if rejection is not None:
                return rejection
        return None
    return JsonRejection(JsonValueReason.NOT_A_JSON_TYPE, path)


def require_json_value(value: object, *, path: str = "$") -> JsonValue:
    """Return ``value`` as :data:`JsonValue`, raising when it is outside the contract."""
    rejection = validate_json_value(value, path=path)
    if rejection is not None:
        raise InvalidToolDefinition(f"{rejection.reason} at {rejection.path}")
    return value  # type: ignore[return-value]


def model_name_suffix_length() -> int:
    """The reserved suffix length, exposed so callers can assert the budget instead of guessing."""
    return len(_ESCAPE_PREFIX) + MODEL_NAME_SUFFIX_HEX_LENGTH


def _fixed_name_prefix(source_ref: ToolSourceRef) -> str:
    if source_ref.is_builtin:
        return _BUILTIN_PREFIX
    return f"{_MCP_PREFIX}{source_ref.source_id}{_NAMESPACE_SEPARATOR}"


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_upstream_name(value: str) -> None:
    if not 1 <= len(value) <= MAX_UPSTREAM_NAME_LENGTH:
        raise InvalidToolDefinition("upstream name length out of bounds")


def _require_model_name(value: str) -> None:
    if not 1 <= len(value) <= MAX_MODEL_NAME_LENGTH:
        raise InvalidToolDefinition("model name length out of bounds")
    if value[0] not in _MODEL_NAME_LEADING:
        raise InvalidToolDefinition("model name must begin alphanumeric")
    if any(character not in _MODEL_NAME_ALPHABET for character in value):
        raise InvalidToolDefinition("model name must be lowercase [a-z0-9_-]")


def _is_lower_hex(value: str) -> bool:
    return all(character in "0123456789abcdef" for character in value)


def merge_risk_hints(hints: Iterable[RiskHints]) -> RiskHints:
    """Combine annotation claims conservatively: the most alarming claim wins.

    Used when several sources describe the same operation. Never used to *grant* anything -- a hint
    cannot remove authority either, and this result is presentation metadata only.
    """
    collected = tuple(hints)
    if not collected:
        return RiskHints()
    return RiskHints(
        read_only=all(hint.read_only for hint in collected),
        destructive=any(hint.destructive for hint in collected),
        idempotent=all(hint.idempotent for hint in collected),
        open_world=any(hint.open_world for hint in collected),
    )
