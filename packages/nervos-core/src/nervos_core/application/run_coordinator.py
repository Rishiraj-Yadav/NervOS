"""Awaited one-shot Run execution coordinator."""

from __future__ import annotations

import asyncio
import time
from typing import Protocol

from nervos_core.application.agents import AgentService
from nervos_core.application.model_completion import (
    MODEL_TIMED_OUT,
    ModelCompletion,
    ModelProviderError,
)
from nervos_core.application.model_providers import ModelProviderCatalog
from nervos_core.application.trusted_chat import ChatOutcome, TrustedAgentHandlerResolver
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.runs import Run

# Upper bound accepted by the signed 64-bit integer column used for elapsed milliseconds.
MAX_ELAPSED_MS = 2**63 - 1


class MonotonicClock(Protocol):
    def __call__(self) -> int:
        """Return a monotonic nanosecond tick used only for duration measurement."""
        ...


def system_monotonic_nanoseconds() -> int:
    """Production monotonic source; never used for persisted timestamps."""
    return time.monotonic_ns()


class RunCoordinator:
    """Execute one trusted Run with exactly one bounded model request.

    The coordinator depends only on application abstractions and immutable domain values.
    It never holds a database session, transaction, or ORM record across the provider
    await, and it never returns a model answer except as the committed succeeded Run.
    """

    def __init__(
        self,
        agents: AgentService,
        handlers: TrustedAgentHandlerResolver,
        providers: ModelProviderCatalog,
        monotonic: MonotonicClock = system_monotonic_nanoseconds,
    ) -> None:
        self._agents = agents
        self._handlers = handlers
        self._providers = providers
        self._monotonic = monotonic

    async def execute(self, owner_user_id: int, agent_instance_id: int, input_text: str) -> Run:
        """Create, start, execute, and terminalize one Run.

        Handler resolution, provider resolution, and provider configuration availability are
        all verified inside `AgentService.create_run` before any Run row is inserted, so a
        known pre-run rejection persists no Run and makes zero model calls.
        """
        created = self._agents.create_run(
            owner_user_id, agent_instance_id, input_text, self._handlers, self._providers
        )
        completion = self._providers.resolve(created.model_provider)
        started = self._agents.start(owner_user_id, created.id)
        return await self._execute_running(owner_user_id, started, completion)

    async def _execute_running(
        self, owner_user_id: int, run: Run, completion: ModelCompletion
    ) -> Run:
        handler = self._handlers.resolve(
            AgentDefinitionId(run.agent_key, run.agent_definition_version)
        )
        start = self._monotonic()
        try:
            async with asyncio.timeout(run.limits.provider_timeout_ms / 1000):
                outcome: ChatOutcome = await handler.run(completion, run, 0)
        except TimeoutError:
            return self._fail(owner_user_id, run, ModelProviderError(MODEL_TIMED_OUT), start)
        except ModelProviderError as error:
            return self._fail(owner_user_id, run, error, start)
        return self._succeed(owner_user_id, run, outcome, start)

    def _succeed(self, owner_user_id: int, run: Run, outcome: ChatOutcome, start: int) -> Run:
        return self._agents.succeed(
            owner_user_id,
            run.id,
            outcome.output_text,
            outcome.finish_reason,
            outcome.usage,
            self._elapsed_ms(start),
        )

    def _fail(self, owner_user_id: int, run: Run, error: ModelProviderError, start: int) -> Run:
        return self._agents.fail(
            owner_user_id,
            run.id,
            error.code,
            error.message,
            error.usage,
            self._elapsed_ms(start),
        )

    def _elapsed_ms(self, start: int) -> int:
        elapsed = self._monotonic() - start
        if elapsed < 0:
            return 0
        return min(elapsed // 1_000_000, MAX_ELAPSED_MS)
