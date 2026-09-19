"""``McpToolExecutor`` normalization: what a call becomes, and what it refuses to become.

The executor is the only place an SDK result turns into a provider-neutral :class:`ToolResult`, so
these tests pin every branch of that mapping. The two that matter most are negative: an image or a
resource is a classified failure rather than something stringified into a model's context, and a
non-conforming ``structuredContent`` is a failure rather than a silently dropped half of an answer.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.types import ImageContent, ResourceLink, TextContent
from nervos_core.application.tool_registry import (
    ToolExecutionFailure,
    ToolFailureReason,
    ToolOutcomeUnknown,
    ToolResult,
)
from nervos_core.domain.tools import JsonValue, RiskHints, ToolDescriptor, ToolSourceKind
from nervos_mcp.errors import McpErrorCode, McpToolError
from nervos_mcp.executor import McpToolExecutor

INPUT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}
OUTPUT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


class _StubGateway:
    """A gateway that returns one scripted result or raises one scripted error."""

    def __init__(self, result: Any = None, error: BaseException | None = None) -> None:
        self._result = result
        self._error = error
        self.calls: list[tuple[int, str, Mapping[str, JsonValue]]] = []

    async def call_tool(
        self, connection_id: int, upstream_name: str, arguments: Mapping[str, JsonValue]
    ) -> Any:
        self.calls.append((connection_id, upstream_name, arguments))
        if self._error is not None:
            raise self._error
        return self._result


def _descriptor(*, output_schema: dict[str, JsonValue] | None = None) -> ToolDescriptor:
    return ToolDescriptor(
        tool_definition_id=1,
        upstream_name="read_document",
        model_name="mcp_1_read_document",
        source_kind=ToolSourceKind.MCP,
        source_id=1,
        display_name="Read Document",
        description="Reads a document.",
        input_schema=INPUT_SCHEMA,
        output_schema=output_schema,
        risk_hints=RiskHints(),
        fingerprint="a" * 64,
    )


def _result(*, content: list[Any], structured: Any = None, is_error: bool = False) -> Any:
    return SimpleNamespace(is_error=is_error, content=content, structured_content=structured)


async def _execute(gateway: _StubGateway, descriptor: ToolDescriptor) -> ToolResult:
    executor = McpToolExecutor(gateway=gateway, connection_id=1)
    return await executor.execute(descriptor, {})


@pytest.mark.anyio
async def test_text_content_becomes_the_tool_result_text() -> None:
    gateway = _StubGateway(_result(content=[TextContent(type="text", text="hello")]))

    executed = await _execute(gateway, _descriptor())

    assert executed == ToolResult(text="hello", structured=None)


@pytest.mark.anyio
async def test_a_server_reported_error_is_a_known_failure_without_remote_text() -> None:
    gateway = _StubGateway(
        _result(content=[TextContent(type="text", text="remote detail")], is_error=True)
    )

    with pytest.raises(ToolExecutionFailure) as failure:
        await _execute(gateway, _descriptor())

    assert failure.value.reason is ToolFailureReason.INTERNAL
    assert "remote detail" not in failure.value.message


@pytest.mark.anyio
async def test_an_unsupported_content_block_is_a_classified_failure() -> None:
    blocks: list[Any] = [
        ImageContent(type="image", data="aGk=", mime_type="image/png"),
        ResourceLink(type="resource_link", name="doc", uri="file:///doc"),
        object(),
    ]
    for block in blocks:
        gateway = _StubGateway(_result(content=[block]))

        with pytest.raises(ToolExecutionFailure) as failure:
            await _execute(gateway, _descriptor())

        assert failure.value.reason is ToolFailureReason.RESULT_UNSUPPORTED


@pytest.mark.anyio
async def test_structured_content_is_dropped_when_no_output_schema_was_advertised() -> None:
    gateway = _StubGateway(
        _result(
            content=[TextContent(type="text", text="hello")],
            structured={"answer": "ok"},
        )
    )

    executed = await _execute(gateway, _descriptor())

    assert executed.structured is None


@pytest.mark.anyio
async def test_structured_content_is_kept_when_it_conforms_to_the_advertised_schema() -> None:
    gateway = _StubGateway(
        _result(
            content=[TextContent(type="text", text="hello")],
            structured={"answer": "ok"},
        )
    )

    executed = await _execute(gateway, _descriptor(output_schema=OUTPUT_SCHEMA))

    assert executed.structured == {"answer": "ok"}


@pytest.mark.anyio
async def test_non_conforming_structured_content_is_refused_rather_than_dropped() -> None:
    gateway = _StubGateway(
        _result(
            content=[TextContent(type="text", text="hello")],
            structured={"answer": 5},
        )
    )

    with pytest.raises(ToolExecutionFailure) as failure:
        await _execute(gateway, _descriptor(output_schema=OUTPUT_SCHEMA))

    assert failure.value.reason is ToolFailureReason.RESULT_UNSUPPORTED


@pytest.mark.anyio
async def test_a_gateway_classified_tool_error_becomes_a_known_failure() -> None:
    gateway = _StubGateway(error=McpToolError(McpErrorCode.TOOL_FAILED))

    with pytest.raises(ToolExecutionFailure) as failure:
        await _execute(gateway, _descriptor())

    assert failure.value.reason is ToolFailureReason.INTERNAL
    assert failure.value.message == McpToolError(McpErrorCode.TOOL_FAILED).message


@pytest.mark.anyio
async def test_an_unknown_outcome_propagates_unchanged() -> None:
    gateway = _StubGateway(error=ToolOutcomeUnknown())

    with pytest.raises(ToolOutcomeUnknown):
        await _execute(gateway, _descriptor())
