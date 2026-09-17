"""D3 registry tests: source-identity keying, static registration, and what the registry is not.

The registry is keyed by the full :class:`ToolSourceRef`, not by kind alone, so several MCP
connections coexist beside the single built-in source and D5 needs no redesign. D3 registers only
``(BUILTIN, None)``; these tests use stand-ins for the other refs, because no MCP implementation
exists in D3.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from nervos_core.application.tool_registry import (
    DuplicateToolSource,
    ToolExecutionFailure,
    ToolFailureReason,
    ToolRegistry,
    ToolResult,
    UnknownToolSource,
)
from nervos_core.domain.tools import JsonValue, ToolDescriptor, ToolSourceKind, ToolSourceRef

BUILTIN = ToolSourceRef(ToolSourceKind.BUILTIN, None)
MCP_12 = ToolSourceRef(ToolSourceKind.MCP, 12)
MCP_27 = ToolSourceRef(ToolSourceKind.MCP, 27)

REGISTRY_SOURCE = (
    Path(__file__).resolve().parents[2] / "src" / "nervos_core" / "application" / "tool_registry.py"
)


class FakeSource:
    def __init__(self, label: str) -> None:
        self.label = label

    async def list_tools(self) -> Sequence[ToolDescriptor]:
        return ()


class FakeExecutor:
    def __init__(self, label: str) -> None:
        self.label = label

    async def execute(
        self, descriptor: ToolDescriptor, arguments: Mapping[str, JsonValue]
    ) -> ToolResult:
        return ToolResult(text=self.label)


def registered(*refs: ToolSourceRef) -> ToolRegistry:
    registry = ToolRegistry()
    for index, ref in enumerate(refs):
        registry.register(
            source_ref=ref,
            source=FakeSource(f"source-{index}"),
            executor=FakeExecutor(f"executor-{index}"),
        )
    return registry


class TestRegistration:
    def test_the_builtin_source_registers_under_builtin_none(self) -> None:
        registry = registered(BUILTIN)
        assert registry.source_refs() == (BUILTIN,)

    def test_several_mcp_sources_of_the_same_kind_coexist(self) -> None:
        # This is the property that keeps D5 from needing a redesign.
        registry = registered(BUILTIN, MCP_12, MCP_27)
        assert set(registry.source_refs()) == {BUILTIN, MCP_12, MCP_27}
        assert registry.source(MCP_12) is not registry.source(MCP_27)

    def test_sources_and_executors_resolve_independently(self) -> None:
        registry = registered(MCP_12, MCP_27)
        assert registry.source(MCP_12) is not registry.executor(MCP_12)
        assert registry.source(MCP_12) is not registry.source(MCP_27)
        assert registry.executor(MCP_12) is not registry.executor(MCP_27)

    def test_registering_one_exact_source_ref_twice_is_refused(self) -> None:
        registry = registered(MCP_12)
        with pytest.raises(DuplicateToolSource):
            registry.register(
                source_ref=MCP_12, source=FakeSource("again"), executor=FakeExecutor("again")
            )

    def test_a_different_source_id_is_not_a_duplicate(self) -> None:
        registry = registered(MCP_12)
        registry.register(
            source_ref=MCP_27, source=FakeSource("other"), executor=FakeExecutor("other")
        )
        assert set(registry.source_refs()) == {MCP_12, MCP_27}

    def test_an_unregistered_source_ref_raises(self) -> None:
        registry = registered(BUILTIN)
        with pytest.raises(UnknownToolSource):
            registry.source(MCP_12)
        with pytest.raises(UnknownToolSource):
            registry.executor(MCP_12)

    def test_an_empty_registry_lists_nothing(self) -> None:
        assert ToolRegistry().source_refs() == ()


class TestRegistryResponsibilities:
    """The registry registers and resolves. It does none of these other jobs."""

    def source_code(self) -> str:
        return REGISTRY_SOURCE.read_text(encoding="utf-8")

    def imported_modules(self) -> set[str]:
        """Real import statements, so prose describing an absence is not mistaken for the thing."""
        tree = ast.parse(self.source_code())
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                modules.add(node.module)
        return modules

    def test_there_is_no_dynamic_plugin_loading(self) -> None:
        modules = self.imported_modules()
        assert not modules & {"importlib", "pkg_resources", "entrypoints"}
        assert "entry_points" not in self.source_code()

    @pytest.mark.parametrize(
        "forbidden",
        ["check_permission", "ModelCompletion", "ToolInvocation", "tool_invocations"],
    )
    def test_the_registry_performs_no_execution_or_permission_work(self, forbidden: str) -> None:
        assert forbidden not in self.source_code()

    def test_the_registry_never_executes_a_tool(self) -> None:
        # `register` stores; it does not call. Nothing in the module awaits a tool.
        assert "await executor" not in self.source_code()


class TestFailureVocabulary:
    def test_the_vocabulary_is_closed_and_has_no_per_tool_member(self) -> None:
        # `json_transform`'s non-object selection is ARGUMENTS_INVALID, not a new member.
        assert {reason.value for reason in ToolFailureReason} == {
            "arguments_invalid",
            "result_too_large",
            "result_unsupported",
            "internal",
        }

    def test_a_failure_carries_a_reason_and_a_safe_message(self) -> None:
        failure = ToolExecutionFailure(ToolFailureReason.ARGUMENTS_INVALID, "Bad arguments.")
        assert failure.reason is ToolFailureReason.ARGUMENTS_INVALID
        assert failure.message == "Bad arguments."


class TestToolResult:
    def test_text_only_results_are_valid(self) -> None:
        assert ToolResult(text="hello").structured is None

    def test_structured_results_must_be_json_safe(self) -> None:
        assert ToolResult(text="t", structured={"a": [1, None, True]}).structured is not None

    @pytest.mark.parametrize(
        "bad",
        [{"n": float("nan")}, {"i": float("inf")}, [object()], {"k": b"bytes"}, {1: "x"}],
    )
    def test_a_non_json_structured_value_is_refused(self, bad: object) -> None:
        from nervos_core.domain.tools import InvalidToolDefinition

        with pytest.raises(InvalidToolDefinition):
            ToolResult(text="t", structured=bad)  # type: ignore[arg-type]
