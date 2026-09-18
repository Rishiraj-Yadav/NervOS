"""Provider-neutral trusted Run execution, extracted from the awaited coordinator.

The executor owns exactly one thing: turning an immutable `running` Run plus one resolved
`ModelCompletion` into a frozen `ExecutionOutcome`. It is deliberately **retry-policy
neutral** — it never decides whether a normalized failure is safe to retry, because retry
policy belongs to Stage C orchestration (`job_execution`), not to the provider execution
primitive. It also never holds a database session, transaction, or ORM record across the
provider await.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Literal, Protocol

from nervos_core.application.model_completion import (
    INTERNAL_EXECUTION_ERROR,
    MODEL_TIMED_OUT,
    ModelCompletion,
    ModelProviderError,
)
from nervos_core.application.tool_invocations import ClaimHandle
from nervos_core.application.trusted_chat import ChatOutcome, TrustedAgentHandlerResolver
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.runs import ModelUsage, Run

# Upper bound accepted by the signed 64-bit integer column used for elapsed milliseconds.
MAX_ELAPSED_MS = 2**63 - 1


class MonotonicClock(Protocol):
    def __call__(self) -> int:
        """Return a monotonic nanosecond tick used only for duration measurement."""
        ...


def system_monotonic_nanoseconds() -> int:
    """Production monotonic source; never used for persisted timestamps."""
    return time.monotonic_ns()


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """One validated, retry-policy-neutral execution result.

    `elapsed_ms` is required rather than derived downstream: the frozen Run terminal shapes
    demand a non-null elapsed time for both `succeeded` and `failed`, so an outcome without
    it could not be persisted at all. `__post_init__` enforces the exact status/nullability
    shape here, inside the executor, so a malformed outcome fails at construction instead of
    surfacing later as an `IntegrityError` from a shape CHECK.
    """

    status: Literal["succeeded", "failed"]
    output_text: str | None
    finish_reason: str | None
    usage: ModelUsage
    elapsed_ms: int
    error_code: str | None
    error_message: str | None

    def __post_init__(self) -> None:
        if not 0 <= self.elapsed_ms <= MAX_ELAPSED_MS:
            raise ValueError("invalid elapsed time")
        if self.status == "succeeded":
            if (
                self.output_text is None
                or self.finish_reason is None
                or self.error_code is not None
                or self.error_message is not None
            ):
                raise ValueError("invalid succeeded outcome")
            return
        if self.output_text is not None or self.finish_reason is not None:
            raise ValueError("invalid failed outcome")
        if self.error_code is None or self.error_message is None:
            raise ValueError("failed outcome requires a safe normalized error")


class ToolLoopHandler(Protocol):
    """The tool-enabled execution shape, which needs the claim its durable writes are fenced on.

    Declared as a separate protocol rather than folded into :class:`TrustedAgentHandler` because the
    tool loop is the only handler that writes durable state, and widening the existing protocol
    would force every tool-free handler to accept an authority it must never use. The claim is typed
    structurally so this module depends on no orchestration module.
    """

    async def run(
        self, completion: ModelCompletion, run: Run, claim: ClaimHandle, elapsed_ms: int
    ) -> ChatOutcome: ...


class RunExecutor:
    """Execute one trusted Run with exactly one bounded model request."""

    def __init__(
        self,
        handlers: TrustedAgentHandlerResolver,
        monotonic: MonotonicClock = system_monotonic_nanoseconds,
        tool_loop: ToolLoopHandler | None = None,
    ) -> None:
        self._handlers = handlers
        self._monotonic = monotonic
        self._tool_loop = tool_loop

    async def execute(
        self, run: Run, completion: ModelCompletion, claim: ClaimHandle | None = None
    ) -> ExecutionOutcome:
        """Run one bounded provider call and normalize its result or failure.

        Every normalized provider failure — including the timeout wrapper — becomes a frozen
        `failed` outcome rather than an exception, exactly as the awaited coordinator behaved.

        A Run whose snapshot allows tools is dispatched to the tool loop, which performs its own
        provider calls under the same outer deadline. A Run whose snapshot allows none takes the
        unchanged single-call path, so `nervos.chat@1` behaves exactly as it did in Stage B/C.
        """
        start = self._monotonic()
        try:
            async with asyncio.timeout(run.limits.provider_timeout_ms / 1000):
                outcome: ChatOutcome = await self._invoke(run, completion, claim)
        except TimeoutError:
            return self._failed(ModelProviderError(MODEL_TIMED_OUT), start)
        except ModelProviderError as error:
            return self._failed(error, start)
        except Exception:
            return self._failed(ModelProviderError(INTERNAL_EXECUTION_ERROR), start)
        return ExecutionOutcome(
            status="succeeded",
            output_text=outcome.output_text,
            finish_reason=outcome.finish_reason,
            usage=outcome.usage,
            elapsed_ms=self.elapsed_ms(start),
            error_code=None,
            error_message=None,
        )

    async def _invoke(
        self, run: Run, completion: ModelCompletion, claim: ClaimHandle | None
    ) -> ChatOutcome:
        """Resolve the one handler this Run's snapshot selects.

        A tool-enabled Run with no claim is a routing defect, not a provider error, so it fails
        closed as an internal execution error rather than silently degrading to a single call the
        Run never authorized.
        """
        if run.limits.max_tool_calls <= 0:
            handler = self._handlers.resolve(
                AgentDefinitionId(run.agent_key, run.agent_definition_version)
            )
            return await handler.run(completion, run, 0)
        if self._tool_loop is None or claim is None:
            raise ModelProviderError(INTERNAL_EXECUTION_ERROR)
        return await self._tool_loop.run(completion, run, claim, 0)

    def _failed(self, error: ModelProviderError, start: int) -> ExecutionOutcome:
        return ExecutionOutcome(
            status="failed",
            output_text=None,
            finish_reason=None,
            usage=error.usage,
            elapsed_ms=self.elapsed_ms(start),
            error_code=error.code,
            error_message=error.message,
        )

    def elapsed_ms(self, start: int) -> int:
        """Return the clamped monotonic duration since `start` in whole milliseconds."""
        elapsed = self._monotonic() - start
        if elapsed < 0:
            return 0
        return min(elapsed // 1_000_000, MAX_ELAPSED_MS)
