"""AgentContext exposes only immutable, Worker-owned read views."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from nervos_sdk import AgentContext, SelectedMemory


def test_context_carries_only_frozen_read_views() -> None:
    context = AgentContext(
        run_id="run-1",
        agent_instance_id="instance-1",
        configuration={"schedule": "09:00", "nested": {"depth": 1}},
        context="rendered context",
        memory=(SelectedMemory(scope="user", content="prefers UTC"),),
    )

    assert context.configuration["schedule"] == "09:00"
    assert context.context == "rendered context"
    assert context.memory[0].scope == "user"
    with pytest.raises(TypeError):
        context.configuration["schedule"] = "10:00"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        context.run_id = "run-2"  # type: ignore[misc]


def test_absent_ports_are_none_and_memory_is_a_tuple() -> None:
    context = AgentContext(
        run_id="run-1",
        agent_instance_id="instance-1",
        memory=[SelectedMemory(scope="agent", content="state")],
    )

    assert context.model is None
    assert context.tools is None
    assert isinstance(context.memory, tuple)
