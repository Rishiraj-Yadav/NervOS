"""Tests for the package host runner, entrypoint loading, health check, and serialized ports."""

# pyright: basic

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from nervos_package_host.runner import (
    HostEntrypointError,
    SerializedModelPort,
    SerializedToolPort,
    health_check,
    load_entrypoint,
    run_entrypoint,
)
from nervos_sdk import AgentContext, AgentResult
from nervos_sdk.types import ModelRequest, ModelResult, ToolRequest, ToolResult


class ValidAgent:
    async def run(self, context: AgentContext) -> AgentResult:
        return AgentResult(final_message=f"Echo: {context.input.get('text', '')}")


class SyncAgent:
    def run(self, context: AgentContext) -> AgentResult:
        return AgentResult(final_message="sync")


def test_entrypoint_loading_and_health_check(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    fake_module = types.ModuleType("fake_agent_mod")
    fake_module.ValidAgent = ValidAgent  # type: ignore[attr-defined]
    fake_module.SyncAgent = SyncAgent  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_agent_mod", fake_module)

    # Health check succeeds on valid async agent
    health_check("fake_agent_mod:ValidAgent")

    # Health check fails on sync agent
    with pytest.raises(HostEntrypointError):
        health_check("fake_agent_mod:SyncAgent")

    # Health check fails on missing symbol
    with pytest.raises(HostEntrypointError):
        health_check("fake_agent_mod:MissingSymbol")


@pytest.mark.anyio
async def test_serialized_model_port_one_in_flight() -> None:
    in_flight = 0
    max_in_flight = 0

    async def fake_complete(request: ModelRequest) -> ModelResult:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return ModelResult(output_text="response")

    port = SerializedModelPort(fake_complete)
    req1 = ModelRequest(messages=())
    req2 = ModelRequest(messages=())

    results = await asyncio.gather(port.complete(req1), port.complete(req2))
    assert len(results) == 2
    assert max_in_flight == 1  # Guaranteed serialized via asyncio.Lock


@pytest.mark.anyio
async def test_serialized_tool_port_one_in_flight() -> None:
    in_flight = 0
    max_in_flight = 0

    async def fake_invoke(request: ToolRequest) -> ToolResult:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return ToolResult(content={"ok": True})

    port = SerializedToolPort(fake_invoke)
    req1 = ToolRequest(name="calc")
    req2 = ToolRequest(name="calc")

    results = await asyncio.gather(port.invoke(req1), port.invoke(req2))
    assert len(results) == 2
    assert max_in_flight == 1
