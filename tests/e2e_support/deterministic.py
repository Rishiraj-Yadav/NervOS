"""Deterministic offline provider doubles shared by the supervised E2E API and Worker.

These doubles live outside every shipped package, so production composition can never reach
them. They perform no network I/O and require no credential: the supervised journey proves the
durable path end to end without contacting any provider.
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from pathlib import Path

from nervos_core.application.model_completion import (
    MODEL_RATE_LIMITED,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    StopOutcome,
    ToolCall,
    ToolResultTurn,
)

ANTHROPIC_ID = "anthropic"
OPENAI_ID = "openai"
ANTHROPIC_REPLY = "Deterministic Anthropic reply from NervOS."
OPENAI_REPLY = "Deterministic OpenAI reply from NervOS."

# Test-only retry delay. Production uses the reviewed 1s/2s/4s schedule; the browser journey
# needs the durable wait to outlast a couple of two-second UI polls so the queued retry is
# observable, while staying far inside the bounded polling budget.
RETRY_DELAY_VARIABLE = "NERVOS_E2E_RETRY_DELAY_SECONDS"


class FixedDelayRetry:
    """Test-only policy with one fixed delay, so the journey can observe a durable wait."""

    def __init__(self, seconds: float) -> None:
        self._delay = timedelta(seconds=seconds)

    def delay(self, ordinal: int) -> timedelta:
        if ordinal < 1:
            raise ValueError("retry ordinal must be positive")
        return self._delay


# Test-only provider-call ledger. The supervisor points this at a path inside its own temporary
# directory and reads it back to prove how many provider invocations actually happened: a
# pre-start Worker loss must contribute zero, and the recovered Run must contribute exactly one.
# Production never sets it, so production never writes a file.
PROVIDER_CALL_LOG_VARIABLE = "NERVOS_E2E_PROVIDER_CALL_LOG"

# Test-only scripted-outcome control. The supervisor points both at paths inside its own
# temporary directory: one names a prompt whose first call(s) must be refused with a normalized
# rate limit, the other records every call so the script knows how many attempts it has already
# seen. Scripting by prompt keeps one Worker able to serve several journeys in one run, and
# keeping the count on disk means a *restarted* Worker still continues the same script — an
# in-memory counter could not, and C4's whole point is that a retry survives a new process.
SCRIPTED_FAILURES_VARIABLE = "NERVOS_E2E_SCRIPTED_FAILURES"
SCRIPTED_CALL_LOG_VARIABLE = "NERVOS_E2E_SCRIPTED_CALL_LOG"

# Test-only blocking gate. The supervisor names one prompt whose provider call must stay open
# until a release file appears, and points the release at a path inside its own temporary
# directory. This is what makes a cancellation journey deterministic instead of timing-dependent:
# the browser can click Cancel knowing the Run is genuinely mid-call, and the supervisor can
# prove the call was *stopped* rather than merely slow.
BLOCK_INPUT_VARIABLE = "NERVOS_E2E_BLOCK_INPUT"
BLOCK_RELEASE_VARIABLE = "NERVOS_E2E_BLOCK_RELEASE"

# Test-only proof-of-stopping ledger. A blocked call that observes `CancelledError` appends one
# line here, which is the only evidence that separates "NervOS stopped waiting" from "the
# provider had not answered yet". Production sets neither variable, so production never blocks
# and never writes a file.
CANCEL_OBSERVED_VARIABLE = "NERVOS_E2E_CANCEL_OBSERVED"

# Test-only tool-turn scripting. The supervisor names one prompt whose model turn must request the
# one granted tool and then conclude. The double never guesses the model-facing tool name: the
# catalog it is actually offered is passed to it, so it calls whatever name that turn offered. It
# decides turn *kind* from the conversation it is handed -- a request that already carries a tool
# result is the concluding turn, so exactly one tool call runs per Run and no state is kept between
# provider invocations. The argument JSON is the built-in `calculate` expression the supervisor
# grants, kept here so the script and the seed cannot drift apart silently.
TOOL_INPUT_VARIABLE = "NERVOS_E2E_TOOL_INPUT"
TOOL_ARGUMENTS_JSON = '{"expression": "6 * 7"}'
TOOL_CALL_ID = "e2e-tool-call-1"


def _record_cancellation_observed(provider_id: str, user_text: str) -> None:
    """Record that a blocked provider call was cancelled out from under the Worker."""
    target = os.environ.get(CANCEL_OBSERVED_VARIABLE, "").strip()
    if not target:
        return
    with Path(target).open("a", encoding="utf-8") as ledger:
        ledger.write(f"{provider_id}\t{user_text}\n")


async def _block_until_released_or_cancelled(provider_id: str, user_text: str) -> None:
    """Hold the one scripted call open until the supervisor releases it or the Worker stops it.

    A cancelled hold is reported before it propagates, because the Worker's decision to stop
    waiting is the behavior under test and it must be observable without reading Worker logs.
    """
    target = os.environ.get(BLOCK_INPUT_VARIABLE, "").strip()
    if not target or target != user_text:
        return
    release = os.environ.get(BLOCK_RELEASE_VARIABLE, "").strip()
    try:
        while not release or not Path(release).exists():
            await asyncio.sleep(0.05)
    except asyncio.CancelledError:
        _record_cancellation_observed(provider_id, user_text)
        raise


def _record_provider_call(provider_id: str) -> None:
    """Append one line per provider invocation, when the supervisor asked for a ledger."""
    target = os.environ.get(PROVIDER_CALL_LOG_VARIABLE, "").strip()
    if not target:
        return
    with Path(target).open("a", encoding="utf-8") as ledger:
        ledger.write(f"{provider_id}\n")


def _scripted_failure_count(provider_id: str, user_text: str) -> int:
    """Return how many of this prompt's first calls the supervisor wants refused."""
    target = os.environ.get(SCRIPTED_FAILURES_VARIABLE, "").strip()
    if not target:
        return 0
    script = Path(target)
    if not script.exists():
        return 0
    for line in script.read_text(encoding="utf-8").splitlines():
        fields = line.split("\t")
        if len(fields) == 3 and fields[0] == provider_id and fields[1] == user_text:
            return int(fields[2])
    return 0


def _record_scripted_call(provider_id: str, user_text: str) -> int:
    """Record one call and return how many calls for this prompt preceded it."""
    target = os.environ.get(SCRIPTED_CALL_LOG_VARIABLE, "").strip()
    if not target:
        return 0
    marker = f"{provider_id}\t{user_text}"
    log = Path(target)
    prior = 0
    if log.exists():
        prior = sum(1 for line in log.read_text(encoding="utf-8").splitlines() if line == marker)
    with log.open("a", encoding="utf-8") as calls:
        calls.write(f"{marker}\n")
    return prior


def _scripted_tool_call(request: ModelRequest) -> ToolCall | None:
    """Return the one tool call the supervisor scripted for this turn, if any.

    Three conditions must all hold, and every one of them is read from the request rather than from
    process state: the prompt is the scripted one, the model was actually offered a tool (the
    grant-filtered catalog decided that), and the conversation has not yet carried a tool result.
    The last condition is what makes the second turn a conclusion instead of an infinite loop.
    """
    target = os.environ.get(TOOL_INPUT_VARIABLE, "").strip()
    if not target or request.user_text != target or not request.tools:
        return None
    if any(isinstance(turn, ToolResultTurn) for turn in request.turns):
        return None
    return ToolCall(
        call_id=TOOL_CALL_ID, name=request.tools[0].name, arguments_json=TOOL_ARGUMENTS_JSON
    )


class DeterministicCompletion:
    """Provider-identifying offline completion used only by supervised E2E."""

    def __init__(self, provider_id: str, reply: str, total_tokens: int | None) -> None:
        self.provider_id = provider_id
        self.reply = reply
        self.total_tokens = total_tokens
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        _record_provider_call(self.provider_id)
        prior = _record_scripted_call(self.provider_id, request.user_text)
        if prior < _scripted_failure_count(self.provider_id, request.user_text):
            raise ModelProviderError(MODEL_RATE_LIMITED)
        await _block_until_released_or_cancelled(self.provider_id, request.user_text)
        tool_call = _scripted_tool_call(request)
        if tool_call is not None:
            return ModelResponse(
                "",
                self.provider_id,
                request.model_name,
                StopOutcome.TOOL_USE,
                ModelUsage(11, 7, self.total_tokens),
                tool_calls=(tool_call,),
            )
        return ModelResponse(
            self.reply,
            self.provider_id,
            request.model_name,
            StopOutcome.STOP,
            ModelUsage(11, 7, self.total_tokens),
        )


def build_deterministic_completions() -> dict[str, DeterministicCompletion]:
    """Return one distinct double per production provider identifier."""
    return {
        ANTHROPIC_ID: DeterministicCompletion(ANTHROPIC_ID, ANTHROPIC_REPLY, None),
        OPENAI_ID: DeterministicCompletion(OPENAI_ID, OPENAI_REPLY, 18),
    }
