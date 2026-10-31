# The initialize frame's tool list must carry exactly the run's granted tools.

from __future__ import annotations

from datetime import UTC, datetime

from nervos_core.application.tool_permissions import ToolGrant
from nervos_core.domain.tools import (
    RiskHints,
    ToolDescriptor,
    ToolSourceKind,
)
from nervos_worker.package_execution import granted_tools_payload


def _descriptor(definition_id: int, name: str) -> ToolDescriptor:
    return ToolDescriptor(
        tool_definition_id=definition_id,
        upstream_name=f"{name}.upstream",
        model_name=name,
        source_kind=ToolSourceKind.BUILTIN,
        source_id=None,
        display_name=name.replace("_", " ").title(),
        description=f"The {name} tool",
        input_schema={"type": "object", "properties": {name: {"type": "string"}}},
        output_schema=None,
        risk_hints=RiskHints(),
        fingerprint=f"{definition_id:064x}",
    )


def _grant(grant_id: int, definition_id: int) -> ToolGrant:
    return ToolGrant(
        id=grant_id,
        agent_instance_id=7,
        tool_definition_id=definition_id,
        reviewed_fingerprint=f"{definition_id:064x}",
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


def test_granted_tools_payload_lists_exactly_the_granted_tools() -> None:
    descriptors = (
        _descriptor(1, "current_time"),
        _descriptor(2, "research_search"),
        _descriptor(3, "dangerous_tool"),
    )
    grants = (_grant(10, 1), _grant(11, 2))

    tools = granted_tools_payload(descriptors=descriptors, grants=grants)

    # The ungranted third tool never appears. ADR 0046: with no alias binding, a
    # builtin grant is presented under its upstream name -- the exact name a
    # tool_request must use for the mediator to accept it.
    names = sorted(name for tool in tools if isinstance((name := tool.get("name")), str))
    assert names == ["current_time.upstream", "research_search.upstream"]
    by_name = {str(tool["name"]): tool for tool in tools}
    assert by_name["current_time.upstream"]["description"] == "The current_time tool"
    schema = by_name["research_search.upstream"]["input_schema"]
    assert isinstance(schema, dict)
    assert schema["type"] == "object"
    assert "dangerous_tool" not in by_name


def test_granted_tools_payload_prefers_the_bound_alias() -> None:
    """An alias binding renames the presented tool to what tool_request accepts."""
    descriptors = (_descriptor(1, "current_time"),)
    grants = (_grant(10, 1),)

    tools = granted_tools_payload(
        descriptors=descriptors,
        grants=grants,
        bindings={"research.time": {"definition_id": 1}},
    )

    assert [tool["name"] for tool in tools] == ["research.time"]


def test_granted_tools_payload_skips_grants_without_a_live_definition() -> None:
    descriptors = (_descriptor(1, "current_time"),)
    grants = (_grant(10, 1), _grant(11, 99))

    tools = granted_tools_payload(descriptors=descriptors, grants=grants)

    assert [tool["name"] for tool in tools] == ["current_time.upstream"]


def test_granted_tools_payload_is_empty_without_grants() -> None:
    descriptors = (_descriptor(1, "current_time"),)

    assert granted_tools_payload(descriptors=descriptors, grants=()) == []
