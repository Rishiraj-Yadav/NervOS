"""The first built-in tools, their executor, and durable reconciliation of their definitions.

Three tools are built, chosen so the architecture is proven before any remote source exists:

* ``current_time`` -- the current instant, from an injected clock, at UTC or a fixed offset;
* ``calculate`` -- a bounded arithmetic grammar, deliberately **not** ``eval``;
* ``json_transform`` -- RFC 6901 selection and projection inside a bounded JSON document.

All three are side-effect-free and credential-free, and none touches the filesystem, a subprocess, a
shell, the network, an environment variable or the database. ``calculate`` and ``json_transform``
are pure functions of their arguments; ``current_time`` is **not** -- it depends on the injected
clock, so two calls with identical arguments legitimately differ.

**A built-in may declare an idempotency invariant because NervOS owns its code.** That guarantee is
never generalised to third-party MCP tools, and nothing here is connected to Stage C4's replay
policy -- D4 owns execution integration.

A :class:`BuiltinToolSpec` is the **pre-persistence** shape: a code-level description that carries
no id and never pretends to be a :class:`ToolDescriptor`. Reconciliation persists it and returns
durable descriptors; :class:`BuiltinToolSource` exposes only those.

**Reconciliation is complete here but deliberately unwired.** Nothing calls it from the Worker or
API composition roots in D3, because D3 has no tool execution path and no user-visible tool
surface. D4 wires the finished service without redesigning it.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import timezone as fixed_timezone
from decimal import ROUND_HALF_EVEN, Decimal, DecimalException, localcontext
from typing import cast

from nervos_core.application.clock import Clock, require_utc
from nervos_core.application.tool_registry import (
    PersistedToolDefinition,
    ToolDefinitionMaterial,
    ToolDefinitionPersistence,
    ToolExecutionFailure,
    ToolFailureReason,
    ToolRegistry,
    ToolResult,
)
from nervos_core.application.tool_schema import (
    CanonicalSchema,
    SchemaRejection,
    canonical_json_size,
    validate_canonical_schema,
    validate_instance,
)
from nervos_core.domain.tools import (
    DefinitionStatus,
    InvalidToolDefinition,
    JsonValue,
    RiskHints,
    ToolDescriptor,
    ToolSourceKind,
    ToolSourceRef,
    canonical_json_text,
    definition_fingerprint,
    model_tool_name,
    require_json_value,
)

BUILTIN_SOURCE_REF = ToolSourceRef(ToolSourceKind.BUILTIN, None)

MAX_EXPRESSION_LENGTH = 256
MAX_EXPRESSION_TOKENS = 64
MAX_OFFSET_MINUTES = 14 * 60
MAX_POINTER_LENGTH = 256
MAX_PROJECTION_KEYS = 32
MAX_JSON_DOCUMENT_BYTES = 65536
DECIMAL_PRECISION = 28
MAX_EXPRESSION_MAGNITUDE = Decimal(10) ** 15

_ARITHMETIC_OPERATORS = "+-*/"
_OFFSET_PATTERN = re.compile(r"([+-])(\d{2}):(\d{2})\Z")

BuiltinHandler = Callable[[Mapping[str, JsonValue]], ToolResult]


@dataclass(frozen=True, slots=True)
class BuiltinToolSpec:
    """A code-level description of one built-in tool. Carries **no** durable id.

    This is the pre-persistence shape. It is not a :class:`ToolDescriptor` and is not
    interchangeable with one, so a built-in can never be mistaken for something that already has
    durable identity.
    """

    upstream_name: str
    display_name: str
    description: str
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue] | None
    risk_hints: RiskHints
    handler: BuiltinHandler


_CURRENT_TIME_INPUT: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "timezone": {
            "type": "string",
            "description": 'Either "UTC" or a fixed offset such as "+05:45".',
        },
    },
    "additionalProperties": False,
}

_CURRENT_TIME_OUTPUT: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "iso8601": {"type": "string"},
        "unix_seconds": {"type": "integer"},
        "timezone": {"type": "string"},
    },
    "required": ["iso8601", "unix_seconds", "timezone"],
    "additionalProperties": False,
}

_CALCULATE_INPUT: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "expression": {
            "type": "string",
            "description": "Arithmetic using + - * /, unary signs and parentheses.",
        },
    },
    "required": ["expression"],
    "additionalProperties": False,
}

_CALCULATE_OUTPUT: dict[str, JsonValue] = {
    "type": "object",
    "properties": {"result": {"type": "string"}},
    "required": ["result"],
    "additionalProperties": False,
}

# `document` and `result` are open objects: `additionalProperties` is absent, so their nested JSON
# content is permitted and traversable. An open object is expressible in the canonical subset with
# no new construct and no implicit ANY type.
_JSON_TRANSFORM_INPUT: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "document": {"type": "object", "properties": {}},
        "pointer": {
            "type": "string",
            "description": "RFC 6901 JSON Pointer into the document.",
        },
        "keys": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["document", "pointer"],
    "additionalProperties": False,
}

_JSON_TRANSFORM_OUTPUT: dict[str, JsonValue] = {
    "type": "object",
    "properties": {"result": {"type": "object", "properties": {}}},
    "required": ["result"],
    "additionalProperties": False,
}


def builtin_tool_specs(*, clock: Clock) -> tuple[BuiltinToolSpec, ...]:
    """The frozen first built-ins. ``clock`` is injected, never read from global time."""
    return (
        BuiltinToolSpec(
            upstream_name="current_time",
            display_name="Current time",
            description="Return the current instant, in UTC or at a fixed offset.",
            input_schema=_CURRENT_TIME_INPUT,
            output_schema=_CURRENT_TIME_OUTPUT,
            risk_hints=RiskHints(
                read_only=True, destructive=False, idempotent=False, open_world=False
            ),
            handler=_make_current_time_handler(clock),
        ),
        BuiltinToolSpec(
            upstream_name="calculate",
            display_name="Calculate",
            description="Evaluate a bounded arithmetic expression exactly.",
            input_schema=_CALCULATE_INPUT,
            output_schema=_CALCULATE_OUTPUT,
            risk_hints=RiskHints(
                read_only=True, destructive=False, idempotent=True, open_world=False
            ),
            handler=_calculate_handler,
        ),
        BuiltinToolSpec(
            upstream_name="json_transform",
            display_name="JSON transform",
            description="Select a JSON object inside a document, optionally projecting named keys.",
            input_schema=_JSON_TRANSFORM_INPUT,
            output_schema=_JSON_TRANSFORM_OUTPUT,
            risk_hints=RiskHints(
                read_only=True, destructive=False, idempotent=True, open_world=False
            ),
            handler=_json_transform_handler,
        ),
    )


def canonical_spec_schemas(spec: BuiltinToolSpec) -> tuple[CanonicalSchema, CanonicalSchema | None]:
    """Prove a spec's schemas are inside the canonical subset, or refuse the spec outright.

    A built-in whose schema is unsupported is a defect in NervOS's own code, so this raises rather
    than letting the definition become ``unsupported_schema``. That status remains meaningful for
    externally discovered tools, not for code we own.
    """
    input_result = validate_canonical_schema(spec.input_schema)
    if isinstance(input_result, SchemaRejection):
        raise InvalidToolDefinition(
            f"built-in {spec.upstream_name!r} has an unsupported input schema:"
            f" {input_result.reason} at {input_result.path}"
        )
    if spec.output_schema is None:
        return input_result, None
    output_result = validate_canonical_schema(spec.output_schema)
    if isinstance(output_result, SchemaRejection):
        raise InvalidToolDefinition(
            f"built-in {spec.upstream_name!r} has an unsupported output schema:"
            f" {output_result.reason} at {output_result.path}"
        )
    return input_result, output_result


def reconcile_builtin_definitions(
    persistence: ToolDefinitionPersistence,
    *,
    specs: Sequence[BuiltinToolSpec],
    now: datetime,
) -> tuple[ToolDescriptor, ...]:
    """Make the durable built-in definitions match ``specs``, and return durable descriptors.

    Idempotent: a spec whose material is already stored byte-for-byte causes **no write at all**, so
    a repeated startup does not churn ``updated_at``. An existing definition keeps its persisted
    ``model_name`` -- the durable model-facing identity is never recomputed or renamed -- and the
    refreshed fingerprint is computed **from that persisted name**.

    ``agent_tool_grants`` is never touched. A material change therefore moves the definition's
    fingerprint away from the stored ``reviewed_fingerprint``, and D2's evaluator denies the call
    until the user re-confirms. No reviewed fingerprint is ever rewritten.
    """
    descriptors: list[ToolDescriptor] = []
    for spec in specs:
        input_schema, output_schema = canonical_spec_schemas(spec)
        existing = persistence.find_builtin(upstream_name=spec.upstream_name)
        # The persisted name wins for an existing definition: renaming it would silently change a
        # durable identity the model has already been offered and the user has already reviewed.
        model_name = (
            model_tool_name(BUILTIN_SOURCE_REF, spec.upstream_name)
            if existing is None
            else existing.material.model_name
        )
        candidate = ToolDefinitionMaterial(
            source_kind=ToolSourceKind.BUILTIN,
            source_id=None,
            upstream_name=spec.upstream_name,
            model_name=model_name,
            display_name=spec.display_name,
            description=spec.description,
            input_schema=canonical_json_text(input_schema.root),
            output_schema=(
                None if output_schema is None else canonical_json_text(output_schema.root)
            ),
            fingerprint=definition_fingerprint(
                model_name=model_name,
                upstream_name=spec.upstream_name,
                description=spec.description,
                input_schema=input_schema.root,
                output_schema=None if output_schema is None else output_schema.root,
                source_kind=ToolSourceKind.BUILTIN,
                source_id=None,
                risk_hints=spec.risk_hints,
            ),
            status=DefinitionStatus.AVAILABLE,
            risk_hints=spec.risk_hints,
        )

        if existing is None:
            tool_definition_id = persistence.insert(candidate, now=now)
            descriptors.append(
                PersistedToolDefinition(tool_definition_id, candidate).to_descriptor()
            )
            continue
        if existing.material == candidate:
            descriptors.append(existing.to_descriptor())
            continue
        persistence.update_material(existing.tool_definition_id, material=candidate, now=now)
        descriptors.append(
            PersistedToolDefinition(existing.tool_definition_id, candidate).to_descriptor()
        )
    return tuple(descriptors)


def create_builtin_tool_registry(
    persistence: ToolDefinitionPersistence, *, clock: Clock
) -> ToolRegistry:
    """Wire the built-in source and executor under ``(BUILTIN, None)``.

    Registration is explicit and static. This does not reconcile and does not grant anything; the
    caller decides when definitions are made durable.
    """
    registry = ToolRegistry()
    registry.register(
        source_ref=BUILTIN_SOURCE_REF,
        source=BuiltinToolSource(persistence),
        executor=BuiltinToolExecutor(builtin_tool_specs(clock=clock)),
    )
    return registry


class BuiltinToolSource:
    """Exposes the durable built-in definitions. Never fabricates a descriptor without an id."""

    def __init__(self, persistence: ToolDefinitionPersistence) -> None:
        self._persistence = persistence

    async def list_tools(self) -> tuple[ToolDescriptor, ...]:
        return tuple(
            persisted.to_descriptor()
            for persisted in self._persistence.list_for_source(source_ref=BUILTIN_SOURCE_REF)
        )


class BuiltinToolExecutor:
    """Executes the built-ins in-process.

    Arguments are validated against the tool's own canonical schema immediately before the handler
    runs, and the handler's declared output is validated afterwards -- a built-in that produced a
    result outside its own schema is a defect in our code, not a tool failure.
    """

    def __init__(self, specs: Sequence[BuiltinToolSpec]) -> None:
        self._specs = {spec.upstream_name: spec for spec in specs}
        self._schemas = {spec.upstream_name: canonical_spec_schemas(spec) for spec in specs}

    async def execute(
        self, descriptor: ToolDescriptor, arguments: Mapping[str, JsonValue]
    ) -> ToolResult:
        spec = self._specs.get(descriptor.upstream_name)
        if spec is None:
            raise ToolExecutionFailure(
                ToolFailureReason.INTERNAL, "No built-in implementation for this definition."
            )
        input_schema, output_schema = self._schemas[descriptor.upstream_name]

        payload = dict(arguments)
        rejection = validate_instance(input_schema, payload)
        if rejection is not None:
            raise ToolExecutionFailure(
                ToolFailureReason.ARGUMENTS_INVALID,
                f"Invalid arguments ({rejection.reason}).",
            )

        result = spec.handler(payload)

        if output_schema is not None:
            output_rejection = validate_instance(output_schema, result.structured)
            if output_rejection is not None:
                raise ToolExecutionFailure(
                    ToolFailureReason.INTERNAL,
                    f"Built-in produced an invalid result ({output_rejection.reason}).",
                )
        return result


def _make_current_time_handler(clock: Clock) -> BuiltinHandler:
    def handler(arguments: Mapping[str, JsonValue]) -> ToolResult:
        requested = arguments.get("timezone", "UTC")
        timezone_text = requested if isinstance(requested, str) else "UTC"
        offset = _parse_utc_offset(timezone_text)
        instant = require_utc(clock())
        local = instant.astimezone(fixed_timezone(offset))
        iso8601 = local.isoformat()
        return ToolResult(
            text=iso8601,
            structured={
                "iso8601": iso8601,
                "unix_seconds": math.floor(instant.timestamp()),
                # Reported in canonical form from the offset that was actually applied, so the three
                # output fields always agree. `-00:00` and `+00:00` both render as `+00:00`,
                # and would contradict an echoed `-00:00`.
                "timezone": _format_utc_offset(offset),
            },
        )

    return handler


def _format_utc_offset(offset: timedelta) -> str:
    """Render a resolved offset canonically: ``"UTC"`` at zero, else ``±HH:MM``."""
    total_minutes = int(offset.total_seconds()) // 60
    if total_minutes == 0:
        return "UTC"
    sign = "-" if total_minutes < 0 else "+"
    magnitude = abs(total_minutes)
    return f"{sign}{magnitude // 60:02d}:{magnitude % 60:02d}"


def _parse_utc_offset(value: str) -> timedelta:
    """Parse ``UTC`` or a ``+HH:MM``/``-HH:MM`` fixed offset, with minute precision.

    No tz database is consulted, so no IANA name is accepted and no dependency is required. The
    bound is a real-world one: offsets range from ``-14:00`` to ``+14:00``, and real zones use
    quarter-hour granularity such as ``+05:45``.
    """
    if value == "UTC":
        return timedelta(0)
    match = _OFFSET_PATTERN.fullmatch(value)
    if match is None:
        raise ToolExecutionFailure(
            ToolFailureReason.ARGUMENTS_INVALID,
            'Timezone must be "UTC" or a fixed offset such as "+05:45".',
        )
    minutes = int(match.group(3))
    total = int(match.group(2)) * 60 + minutes
    if minutes > 59 or total > MAX_OFFSET_MINUTES:
        raise ToolExecutionFailure(
            ToolFailureReason.ARGUMENTS_INVALID, "Timezone offset is out of range."
        )
    return timedelta(minutes=-total if match.group(1) == "-" else total)


def _calculate_handler(arguments: Mapping[str, JsonValue]) -> ToolResult:
    expression = arguments.get("expression")
    if not isinstance(expression, str):
        raise ToolExecutionFailure(ToolFailureReason.ARGUMENTS_INVALID, "Expression must be text.")
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise ToolExecutionFailure(ToolFailureReason.ARGUMENTS_INVALID, "Expression is too long.")

    tokens = _tokenize(expression)
    if not tokens:
        raise ToolExecutionFailure(ToolFailureReason.ARGUMENTS_INVALID, "Expression is empty.")
    if len(tokens) > MAX_EXPRESSION_TOKENS:
        raise ToolExecutionFailure(
            ToolFailureReason.ARGUMENTS_INVALID, "Expression is too complex."
        )

    try:
        with localcontext() as context:
            # A LOCAL context only. Mutating the process global would leak precision and rounding
            # into every other Decimal consumer, and would make this tool unsafe to re-enter.
            context.prec = DECIMAL_PRECISION
            context.rounding = ROUND_HALF_EVEN
            value = _ExpressionParser(tokens).parse()
            text = _format_decimal(value)
    except DecimalException as error:
        raise ToolExecutionFailure(
            ToolFailureReason.ARGUMENTS_INVALID, "Expression could not be evaluated."
        ) from error
    return ToolResult(text=text, structured={"result": text})


def _json_transform_handler(arguments: Mapping[str, JsonValue]) -> ToolResult:
    document = arguments.get("document")
    pointer = arguments.get("pointer")
    keys = arguments.get("keys")
    if not isinstance(document, dict) or not isinstance(pointer, str):
        raise ToolExecutionFailure(ToolFailureReason.ARGUMENTS_INVALID, "Invalid arguments.")
    if len(pointer) > MAX_POINTER_LENGTH:
        raise ToolExecutionFailure(ToolFailureReason.ARGUMENTS_INVALID, "Pointer is too long.")

    bounded_document = cast("dict[str, JsonValue]", document)
    document_value = require_json_value(bounded_document)
    if canonical_json_size(document_value) > MAX_JSON_DOCUMENT_BYTES:
        raise ToolExecutionFailure(ToolFailureReason.RESULT_TOO_LARGE, "Document is too large.")

    selected = _resolve_pointer(bounded_document, pointer)
    if not isinstance(selected, dict):
        raise ToolExecutionFailure(
            ToolFailureReason.ARGUMENTS_INVALID, "Selected JSON value must be object."
        )

    if keys is not None:
        if not isinstance(keys, list) or len(keys) > MAX_PROJECTION_KEYS:
            raise ToolExecutionFailure(
                ToolFailureReason.ARGUMENTS_INVALID, "Too many requested keys."
            )
        seen: set[str] = set()
        projected: dict[str, JsonValue] = {}
        for key in keys:
            if not isinstance(key, str) or key in seen:
                raise ToolExecutionFailure(
                    ToolFailureReason.ARGUMENTS_INVALID, "Requested keys must be unique strings."
                )
            seen.add(key)
            # An absent key is an error, never a silent omission: a caller must not mistake a
            # dropped field for one that was never there.
            if key not in selected:
                raise ToolExecutionFailure(
                    ToolFailureReason.ARGUMENTS_INVALID,
                    "Requested key is not present in the selected object.",
                )
            projected[key] = selected[key]
        selected = projected

    result_value = require_json_value(cast("JsonValue", selected))
    if canonical_json_size(result_value) > MAX_JSON_DOCUMENT_BYTES:
        raise ToolExecutionFailure(ToolFailureReason.RESULT_TOO_LARGE, "Result is too large.")
    # The caller's document is never mutated: selection returns existing values and projection
    # builds a new mapping.
    return ToolResult(text=canonical_json_text(result_value), structured={"result": result_value})


def _resolve_pointer(document: dict[str, JsonValue], pointer: str) -> object:
    """Resolve an RFC 6901 pointer against an already-bounded document."""
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise ToolExecutionFailure(
            ToolFailureReason.ARGUMENTS_INVALID, "Pointer must be empty or start with '/'."
        )
    current: object = document
    for segment in _split_pointer(pointer):
        if isinstance(current, dict):
            if segment not in current:
                raise ToolExecutionFailure(
                    ToolFailureReason.ARGUMENTS_INVALID, "Pointer does not resolve."
                )
            current = current[segment]
        elif isinstance(current, list):
            index = _parse_array_index(segment)
            if index >= len(current):
                raise ToolExecutionFailure(
                    ToolFailureReason.ARGUMENTS_INVALID, "Pointer does not resolve."
                )
            current = current[index]
        else:
            raise ToolExecutionFailure(
                ToolFailureReason.ARGUMENTS_INVALID, "Pointer does not resolve."
            )
    return current


def _split_pointer(pointer: str) -> list[str]:
    return [_unescape_pointer_segment(segment) for segment in pointer[1:].split("/")]


def _unescape_pointer_segment(segment: str) -> str:
    """Decode the two RFC 6901 escapes. A malformed ``~`` is rejected, never passed through."""
    if "~" not in segment:
        return segment
    decoded: list[str] = []
    index = 0
    while index < len(segment):
        character = segment[index]
        if character != "~":
            decoded.append(character)
            index += 1
            continue
        if index + 1 >= len(segment) or segment[index + 1] not in "01":
            raise ToolExecutionFailure(
                ToolFailureReason.ARGUMENTS_INVALID, "Pointer contains a malformed '~' escape."
            )
        decoded.append("~" if segment[index + 1] == "0" else "/")
        index += 2
    return "".join(decoded)


def _parse_array_index(segment: str) -> int:
    """Parse the RFC 6901 array-index form: ``0``, or a non-zero decimal with no leading zeroes."""
    malformed = not segment or not all("0" <= character <= "9" for character in segment)
    if malformed or (len(segment) > 1 and segment[0] == "0"):
        raise ToolExecutionFailure(
            ToolFailureReason.ARGUMENTS_INVALID,
            "Array index must be a decimal integer without leading zeroes.",
        )
    return int(segment)


class _ExpressionParser:
    """Recursive-descent parser and evaluator over an explicit token stream.

    There is no ``eval``, no ``exec``, no ``ast`` execution and no third-party parser anywhere in
    this path: the grammar is small, closed and hand-written, so nothing in the input can reach
    the interpreter.
    """

    __slots__ = ("_index", "_tokens")

    def __init__(self, tokens: Sequence[tuple[str, str]]) -> None:
        self._tokens = tokens
        self._index = 0

    def parse(self) -> Decimal:
        value = self._expression()
        if self._index != len(self._tokens):
            raise ToolExecutionFailure(
                ToolFailureReason.ARGUMENTS_INVALID, "Unexpected trailing input in expression."
            )
        return _bounded(value)

    def _expression(self) -> Decimal:
        value = self._term()
        while self._peek_operator() in ("+", "-"):
            symbol = self._take_operator()
            right = self._term()
            value = value + right if symbol == "+" else value - right
        return value

    def _term(self) -> Decimal:
        value = self._factor()
        while self._peek_operator() in ("*", "/"):
            symbol = self._take_operator()
            right = self._factor()
            if symbol == "*":
                value = value * right
            else:
                if right == 0:
                    raise ToolExecutionFailure(
                        ToolFailureReason.ARGUMENTS_INVALID, "Division by zero is not allowed."
                    )
                value = value / right
        return value

    def _factor(self) -> Decimal:
        symbol = self._peek_operator()
        if symbol in ("+", "-"):
            self._take_operator()
            value = self._factor()
            return value if symbol == "+" else -value
        return self._primary()

    def _primary(self) -> Decimal:
        if self._index >= len(self._tokens):
            raise ToolExecutionFailure(
                ToolFailureReason.ARGUMENTS_INVALID, "Expression ended unexpectedly."
            )
        kind, text = self._tokens[self._index]
        if kind == "number":
            self._index += 1
            return _bounded(Decimal(text))
        if kind == "lparen":
            self._index += 1
            value = self._expression()
            if self._index >= len(self._tokens) or self._tokens[self._index][0] != "rparen":
                raise ToolExecutionFailure(
                    ToolFailureReason.ARGUMENTS_INVALID, "Missing closing parenthesis."
                )
            self._index += 1
            return value
        raise ToolExecutionFailure(
            ToolFailureReason.ARGUMENTS_INVALID, "Unexpected token in expression."
        )

    def _peek_operator(self) -> str | None:
        if self._index >= len(self._tokens):
            return None
        kind, text = self._tokens[self._index]
        return text if kind == "operator" else None

    def _take_operator(self) -> str:
        _, text = self._tokens[self._index]
        self._index += 1
        return text


def _tokenize(expression: str) -> list[tuple[str, str]]:
    """Split an expression into ``number``, ``operator``, ``lparen`` and ``rparen`` tokens.

    Only ASCII digits form numbers, so a non-ASCII digit is an unsupported character rather than a
    silently accepted literal. No exponent notation is part of the grammar.
    """
    tokens: list[tuple[str, str]] = []
    index = 0
    length = len(expression)
    while index < length:
        character = expression[index]
        if character.isspace():
            index += 1
            continue
        if character in _ARITHMETIC_OPERATORS:
            tokens.append(("operator", character))
            index += 1
            continue
        if character == "(":
            tokens.append(("lparen", character))
            index += 1
            continue
        if character == ")":
            tokens.append(("rparen", character))
            index += 1
            continue
        if "0" <= character <= "9":
            start = index
            while index < length and "0" <= expression[index] <= "9":
                index += 1
            if index < length and expression[index] == ".":
                index += 1
                fraction_start = index
                while index < length and "0" <= expression[index] <= "9":
                    index += 1
                if index == fraction_start:
                    raise ToolExecutionFailure(
                        ToolFailureReason.ARGUMENTS_INVALID, "Malformed number literal."
                    )
            tokens.append(("number", expression[start:index]))
            continue
        raise ToolExecutionFailure(
            ToolFailureReason.ARGUMENTS_INVALID, "Unsupported character in expression."
        )
    return tokens


def _bounded(value: Decimal) -> Decimal:
    """Reject a literal or result outside the magnitude bound, or one that is not finite."""
    if not value.is_finite() or abs(value) > MAX_EXPRESSION_MAGNITUDE:
        raise ToolExecutionFailure(
            ToolFailureReason.ARGUMENTS_INVALID, "Magnitude is out of bounds."
        )
    return value


def _format_decimal(value: Decimal) -> str:
    """Canonical fixed-point text: no exponent notation, no trailing zeros, and no negative zero."""
    if not value.is_finite():
        raise ToolExecutionFailure(
            ToolFailureReason.ARGUMENTS_INVALID, "Result is not a finite number."
        )
    if value == 0:
        # `format(Decimal("-0"), "f")` is "-0", so normalise here and only here.
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text
