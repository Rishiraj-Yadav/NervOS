from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError, replace
from types import MappingProxyType

import pytest
from nervos_sdk import (
    SDK_API_VERSION,
    AgentContext,
    AgentEntrypoint,
    AgentResult,
    JSONValue,
    ModelMessage,
    ModelPort,
    ModelRequest,
    ModelResult,
    ToolPort,
    ToolRequest,
    ToolResult,
)


def test_public_imports_and_version() -> None:
    assert SDK_API_VERSION == "0.1"
    assert AgentContext.__module__ == "nervos_sdk.types"
    assert AgentResult.__module__ == "nervos_sdk.types"
    assert AgentEntrypoint.__module__ == "nervos_sdk.entrypoint"
    assert ModelPort.__module__ == "nervos_sdk.ports"
    assert ToolPort.__module__ == "nervos_sdk.ports"


def test_no_core_dependency_imported_by_public_sdk() -> None:
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "src" / "nervos_sdk"
    imported: set[str] = set()
    for path in source.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

    assert not any(module.startswith("nervos_core") for module in imported)


def test_agent_context_is_frozen_and_copy_safe() -> None:
    nested_items: JSONValue = ["a", "b"]
    original_input: dict[str, JSONValue] = {"prompt": "hello", "nested": {"items": nested_items}}
    original_metadata: dict[str, JSONValue] = {"source": "test"}

    context = AgentContext(
        run_id="run-1",
        agent_instance_id="agent-1",
        input=original_input,
        metadata=original_metadata,
    )

    original_input["prompt"] = "mutated"
    assert isinstance(nested_items, list)
    nested_items.append("c")
    original_metadata["source"] = "mutated"

    assert context.input["prompt"] == "hello"
    nested = context.input["nested"]
    assert isinstance(nested, MappingProxyType)
    assert nested["items"] == ("a", "b")
    assert context.metadata["source"] == "test"

    with pytest.raises(FrozenInstanceError):
        context.run_id = "new-run"  # type: ignore[misc]
    with pytest.raises(TypeError):
        context.input["prompt"] = "new"  # type: ignore[index]

    copied = replace(context, metadata={"copy": True})
    assert copied.metadata["copy"] is True
    with pytest.raises(TypeError):
        copied.metadata["copy"] = False  # type: ignore[index]


def test_agent_result_is_frozen_and_copy_safe() -> None:
    citations: JSONValue = [1, 2]
    output: dict[str, JSONValue] = {"answer": "ok", "citations": citations}
    metadata: dict[str, JSONValue] = {"duration_ms": 10}

    result = AgentResult(output=output, final_message="done", metadata=metadata)

    output["answer"] = "changed"
    assert isinstance(citations, list)
    citations.append(3)
    metadata["duration_ms"] = 99

    assert result.output["answer"] == "ok"
    assert result.output["citations"] == (1, 2)
    assert result.metadata["duration_ms"] == 10

    with pytest.raises(FrozenInstanceError):
        result.final_message = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        result.output["answer"] = "changed"  # type: ignore[index]


def test_model_and_tool_types_are_transport_neutral_and_copy_safe() -> None:
    messages = [ModelMessage(role="user", content="Hi")]
    request = ModelRequest(messages=messages, metadata={"tags": ["unit"]})
    tool_request = ToolRequest(name="lookup", arguments={"query": "x"})
    tool_result = ToolResult(content={"value": {"ok": True}})
    model_result = ModelResult(output_text="Hello", usage={"output_tokens": 3})

    messages.append(ModelMessage(role="assistant", content="mutated"))

    assert request.messages == (ModelMessage(role="user", content="Hi"),)
    assert request.metadata["tags"] == ("unit",)
    assert tool_request.arguments["query"] == "x"
    assert isinstance(tool_result.content["value"], MappingProxyType)
    assert model_result.usage["output_tokens"] == 3

    with pytest.raises(TypeError):
        request.metadata["tags"] = []  # type: ignore[index]
    with pytest.raises(TypeError):
        tool_request.arguments["query"] = "y"  # type: ignore[index]
    with pytest.raises(TypeError):
        model_result.usage["output_tokens"] = 4  # type: ignore[index]


def test_agent_entrypoint_protocol_is_async_run_only() -> None:
    async def run_agent() -> AgentResult:
        class ExampleEntrypoint:
            async def run(self, context: AgentContext) -> AgentResult:
                return AgentResult(output={"run_id": context.run_id})

        entrypoint: AgentEntrypoint = ExampleEntrypoint()
        return await entrypoint.run(AgentContext(run_id="run-1", agent_instance_id="agent-1"))

    signature = inspect.signature(AgentEntrypoint.run)

    assert list(signature.parameters) == ["self", "context"]
    assert inspect.iscoroutinefunction(AgentEntrypoint.run)
    assert inspect.iscoroutinefunction(ModelPort.complete)
    assert inspect.iscoroutinefunction(ToolPort.invoke)

    import asyncio

    result = asyncio.run(run_agent())
    assert result.output["run_id"] == "run-1"
