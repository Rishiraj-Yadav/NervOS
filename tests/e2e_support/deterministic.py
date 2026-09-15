"""Deterministic offline provider doubles shared by the supervised E2E API and Worker.

These doubles live outside every shipped package, so production composition can never reach
them. They perform no network I/O and require no credential: the supervised journey proves the
durable path end to end without contacting any provider.
"""

from __future__ import annotations

from nervos_core.application.model_completion import (
    ModelRequest,
    ModelResponse,
    ModelUsage,
    StopOutcome,
)

ANTHROPIC_ID = "anthropic"
OPENAI_ID = "openai"
ANTHROPIC_REPLY = "Deterministic Anthropic reply from NervOS."
OPENAI_REPLY = "Deterministic OpenAI reply from NervOS."


class DeterministicCompletion:
    """Provider-identifying offline completion used only by supervised E2E."""

    def __init__(self, provider_id: str, reply: str, total_tokens: int | None) -> None:
        self.provider_id = provider_id
        self.reply = reply
        self.total_tokens = total_tokens
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
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
