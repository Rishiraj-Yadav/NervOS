"""``McpToolExecutor``: one ``tools/call``, normalized, with no SDK object escaping.

The executor is the narrow waist between D4 and the MCP SDK, and three of its properties are the
point of it:

**It classifies rather than reports.** A result becomes a :class:`ToolResult`, a
:class:`ToolExecutionFailure`, or :class:`ToolOutcomeUnknown`. It never lets a remote payload,
an SDK object, or an exception's text reach the loop.

**It distinguishes "did not happen" from "may have happened".** Everything that can be refused from
configuration or durable state is refused before the call is attempted, and is therefore a *known*
failure. Once ``call_tool`` has been entered, an exception does not prove that nothing was sent, so
the only safe reading is :class:`ToolOutcomeUnknown` -- which the loop turns into a durable
``ambiguous`` invocation and a failed Run, never into a retry. Text is never inspected to decide
this: a human-readable message is not evidence about a socket.

**It adds no retry and no authorization.** D4 owns the deadline and D2 owns the permission decision;
duplicating either here would create a second authority.

Results are normalized per D5's accepted mapping: text is accepted, a validated
``structuredContent`` is accepted when the definition advertised an output schema, and anything
else -- an image, audio, a
resource, a link, an input-required round -- is a classified ``RESULT_UNSUPPORTED`` failure rather
than something stringified into the model's context.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mcp.types import TextContent
from nervos_core.application.tool_registry import (
    ToolExecutionFailure,
    ToolFailureReason,
    ToolResult,
)
from nervos_core.application.tool_schema import (
    SchemaRejection,
    validate_canonical_schema,
    validate_instance,
)
from nervos_core.domain.tools import JsonValue, ToolDescriptor, require_json_value

from nervos_mcp.errors import McpToolError

# The static observations a failing call may put in front of a model. They name the condition and
# nothing about the remote side: a server's own error text is remote input, and it is dropped rather
# than bounded.
_UNSUPPORTED_CONTENT_NOTE = "Tool returned an unsupported content type."
_UNSUPPORTED_RESULT_NOTE = "Tool returned an unsupported result."
_TOOL_FAILED_NOTE = "The MCP tool reported a failure."


class McpToolExecutor:
    """Execute one already-authorized call against one durable MCP descriptor."""

    def __init__(self, *, gateway: Any, connection_id: int) -> None:
        self._gateway = gateway
        self._connection_id = connection_id

    async def execute(
        self, descriptor: ToolDescriptor, arguments: Mapping[str, JsonValue]
    ) -> ToolResult:
        """Call one tool and normalize the result, or classify why it could not be."""
        try:
            result = await self._gateway.call_tool(
                self._connection_id, descriptor.upstream_name, dict(arguments)
            )
        except McpToolError as error:
            # A gateway-classified remote failure: known, so the invocation is `failed` rather than
            # ambiguous, and the model sees the static sentence only.
            raise ToolExecutionFailure(ToolFailureReason.INTERNAL, error.message) from error
        # ToolOutcomeUnknown and ToolExecutionFailure from the gateway propagate unchanged: both
        # are already the provider-neutral form the loop consumes.
        if result.is_error:
            # The server answered, and said the call failed. That is a known outcome, not an
            # ambiguous one -- and the server's own error text is not repeated to the model.
            raise ToolExecutionFailure(ToolFailureReason.INTERNAL, _TOOL_FAILED_NOTE)
        return _normalize_content(result.content, result.structured_content, descriptor)


def _normalize_content(
    content: Any, structured_content: Any, descriptor: ToolDescriptor
) -> ToolResult:
    """Map one server result onto the supported ``ToolResult`` shape, or refuse it."""
    text_parts: list[str] = []
    for block in content:
        if isinstance(block, TextContent):
            text_parts.append(block.text)
            continue
        # Image, audio, resource and resource_link blocks are all unsupported in Stage D. None of
        # them is stringified and none is base64-encoded into the model's context.
        raise ToolExecutionFailure(ToolFailureReason.RESULT_UNSUPPORTED, _UNSUPPORTED_CONTENT_NOTE)
    structured = _validated_structured(structured_content, descriptor)
    return ToolResult(text="".join(text_parts), structured=structured)


def _validated_structured(structured_content: Any, descriptor: ToolDescriptor) -> JsonValue | None:
    """Accept ``structuredContent`` only when an admitted output schema proves its shape.

    With no advertised output schema there is nothing to validate against, so the value is not
    trusted as typed data and is discarded rather than passed through unchecked. When a schema *was*
    advertised, a value that does not conform is a failure rather than something to drop silently:
    a model told a tool succeeded, while the structured half of its answer was thrown away, would be
    reasoning over a result NervOS had already rejected.
    """
    if structured_content is None or descriptor.output_schema is None:
        return None
    schema = validate_canonical_schema(descriptor.output_schema)
    if isinstance(schema, SchemaRejection):  # pragma: no cover - admission proved this canonical
        raise ToolExecutionFailure(ToolFailureReason.RESULT_UNSUPPORTED, _UNSUPPORTED_RESULT_NOTE)
    value = structured_content
    try:
        require_json_value(value, path="$.structured")
    except Exception as error:
        raise ToolExecutionFailure(
            ToolFailureReason.RESULT_UNSUPPORTED, _UNSUPPORTED_RESULT_NOTE
        ) from error
    if validate_instance(schema, value) is not None:
        raise ToolExecutionFailure(ToolFailureReason.RESULT_UNSUPPORTED, _UNSUPPORTED_RESULT_NOTE)
    return value


__all__ = ["McpToolExecutor"]
