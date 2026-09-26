"""Public port protocols exposed to NervOS agents."""

from __future__ import annotations

from typing import Protocol

from nervos_sdk.types import ModelRequest, ModelResult, ToolRequest, ToolResult


class ModelPort(Protocol):
    """Narrow async model capability available to an agent."""

    async def complete(self, request: ModelRequest) -> ModelResult: ...


class ToolPort(Protocol):
    """Narrow async tool capability available to an agent."""

    async def invoke(self, request: ToolRequest) -> ToolResult: ...
