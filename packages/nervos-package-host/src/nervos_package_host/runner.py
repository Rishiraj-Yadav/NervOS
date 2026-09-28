"""Entrypoint loading, health checks, SDK context execution, and serialized port proxies."""

from __future__ import annotations

import asyncio
import importlib
import inspect
from collections.abc import Awaitable, Callable
from typing import cast

from nervos_sdk import AgentContext, AgentResult, ModelPort, ToolPort
from nervos_sdk.types import ModelRequest, ModelResult, ToolRequest, ToolResult


class HostEntrypointError(ValueError):
    """An installed package entrypoint does not satisfy the G3 host contract."""


def load_entrypoint(value: str) -> object:
    module_name, separator, symbol_name = value.partition(":")
    if not separator or not module_name or not symbol_name:
        raise HostEntrypointError("entrypoint must be module.path:Symbol")
    module = importlib.import_module(module_name)
    try:
        value_object = getattr(module, symbol_name)
    except AttributeError as error:
        raise HostEntrypointError("entrypoint symbol does not exist") from error
    entrypoint = value_object() if inspect.isclass(value_object) else value_object
    run = getattr(entrypoint, "run", None)
    if run is None or not callable(run) or not inspect.iscoroutinefunction(run):
        raise HostEntrypointError("entrypoint run member must be async callable")
    parameters = tuple(inspect.signature(run).parameters.values())
    if len(parameters) != 1:
        raise HostEntrypointError("entrypoint run member must accept one AgentContext")
    return entrypoint


def health_check(value: str) -> None:
    """Load and structurally validate an entrypoint without invoking AgentEntrypoint.run."""
    load_entrypoint(value)


class SerializedModelPort(ModelPort):
    def __init__(self, callback: Callable[[ModelRequest], Awaitable[ModelResult]]) -> None:
        self._callback = callback
        self._lock = asyncio.Lock()

    async def complete(self, request: ModelRequest) -> ModelResult:
        async with self._lock:
            return await self._callback(request)


class SerializedToolPort(ToolPort):
    def __init__(self, callback: Callable[[ToolRequest], Awaitable[ToolResult]]) -> None:
        self._callback = callback
        self._lock = asyncio.Lock()

    async def invoke(self, request: ToolRequest) -> ToolResult:
        async with self._lock:
            return await self._callback(request)


async def run_entrypoint(entrypoint: str, context: AgentContext) -> AgentResult:
    loaded = load_entrypoint(entrypoint)
    result = await cast("Callable[[AgentContext], Awaitable[object]]", loaded.run)(context)  # type: ignore[attr-defined]
    if not isinstance(result, AgentResult):
        raise HostEntrypointError("entrypoint must return AgentResult")
    return result
