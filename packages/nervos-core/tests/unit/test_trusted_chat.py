"""Trusted `nervos.chat@1` handler behavior tests."""

from datetime import UTC, datetime

import pytest
from nervos_core.application.model_completion import (
    MODEL_OUTPUT_INCOMPLETE,
    MODEL_OUTPUT_TOO_LARGE,
    MODEL_REFUSED,
    MODEL_RESPONSE_INVALID,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    StopOutcome,
)
from nervos_core.application.trusted_chat import (
    CHAT_DEFINITION_ID,
    NERVOS_CHAT_SYSTEM_INSTRUCTION,
    BuiltInTrustedAgentHandlerRegistry,
    DuplicateAgentHandler,
    NervosChatHandler,
    UnknownAgentHandler,
    create_builtin_handler_registry,
)
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.runs import (
    STAGE_B_LIMITS,
    ModelUsage,
    Run,
    RunLimits,
    RunStatus,
)

CREATED = datetime(2026, 1, 1, tzinfo=UTC)
STARTED = datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)


class _FakeCompletion:
    def __init__(self, response: ModelResponse | None = None, error: Exception | None = None):
        self.requests: list[ModelRequest] = []
        self._response = response
        self._error = error

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


def _running_run(limits: RunLimits = STAGE_B_LIMITS) -> Run:
    return Run(
        7,
        3,
        "nervos.chat",
        "1",
        "test-provider",
        "opaque/model",
        "hello",
        limits,
        RunStatus.RUNNING,
        CREATED,
        STARTED,
    )


def _response(
    text: str = "answer",
    *,
    provider: str = "test-provider",
    model: str = "opaque/model",
    finish: StopOutcome = StopOutcome.STOP,
    usage: ModelUsage | None = None,
) -> ModelResponse:
    return ModelResponse(text, provider, model, finish, usage)


@pytest.mark.anyio
async def test_handler_sends_exactly_one_narrow_request() -> None:
    completion = _FakeCompletion(_response())
    outcome = await NervosChatHandler().run(completion, _running_run(), 0)
    assert len(completion.requests) == 1
    request = completion.requests[0]
    assert request.system_instruction == NERVOS_CHAT_SYSTEM_INSTRUCTION
    assert request.user_text == "hello"
    assert request.model_name == "opaque/model"
    assert request.max_output_tokens == STAGE_B_LIMITS.max_output_tokens
    assert request.timeout_ms == STAGE_B_LIMITS.provider_timeout_ms
    assert not hasattr(request, "messages")
    assert outcome.output_text == "answer"
    assert outcome.finish_reason == "stop"


@pytest.mark.anyio
async def test_fixed_instruction_is_exactly_versioned_text() -> None:
    assert (
        NERVOS_CHAT_SYSTEM_INSTRUCTION
        == "You are a helpful assistant. Answer the user's request directly and accurately."
    )


@pytest.mark.anyio
async def test_snapshot_model_and_limits_drive_the_request_not_current_defaults() -> None:
    snapshot = RunLimits(
        input_max_bytes=8000,
        input_max_code_points=4000,
        output_max_bytes=32000,
        output_max_code_points=16000,
        provider_timeout_ms=5000,
        max_output_tokens=64,
        max_model_calls=1,
    )
    completion = _FakeCompletion(_response())
    await NervosChatHandler().run(completion, _running_run(snapshot), 0)
    request = completion.requests[0]
    assert request.max_output_tokens == 64
    assert request.timeout_ms == 5000


@pytest.mark.anyio
async def test_usage_is_optional_and_defaults_to_empty_usage() -> None:
    completion = _FakeCompletion(_response(usage=None))
    outcome = await NervosChatHandler().run(completion, _running_run(), 0)
    assert outcome.usage == ModelUsage()


@pytest.mark.anyio
async def test_started_at_and_multiline_input_are_preserved_exactly() -> None:
    run = Run(
        7,
        3,
        "nervos.chat",
        "1",
        "test-provider",
        "opaque/model",
        "line one\n\tline two\n",
        STAGE_B_LIMITS,
        RunStatus.RUNNING,
        CREATED,
        STARTED,
    )
    completion = _FakeCompletion(_response())
    await NervosChatHandler().run(completion, run, 0)
    assert completion.requests[0].user_text == "line one\n\tline two\n"


@pytest.mark.parametrize(
    ("finish", "expected_code"),
    [
        (StopOutcome.INCOMPLETE, MODEL_OUTPUT_INCOMPLETE),
        (StopOutcome.REFUSED, MODEL_REFUSED),
        (StopOutcome.INVALID, MODEL_RESPONSE_INVALID),
    ],
)
@pytest.mark.anyio
async def test_non_stop_outcomes_are_execution_failures(
    finish: StopOutcome, expected_code: str
) -> None:
    completion = _FakeCompletion(_response(finish=finish))
    with pytest.raises(ModelProviderError) as error:
        await NervosChatHandler().run(completion, _running_run(), 0)
    assert error.value.code == expected_code
    assert len(completion.requests) == 1


@pytest.mark.anyio
async def test_blank_output_is_rejected() -> None:
    completion = _FakeCompletion(_response(text="   \t\n"))
    with pytest.raises(ModelProviderError) as error:
        await NervosChatHandler().run(completion, _running_run(), 0)
    assert error.value.code == MODEL_RESPONSE_INVALID


@pytest.mark.anyio
async def test_nul_output_is_rejected() -> None:
    completion = _FakeCompletion(_response(text="answer\x00"))
    with pytest.raises(ModelProviderError) as error:
        await NervosChatHandler().run(completion, _running_run(), 0)
    assert error.value.code == MODEL_RESPONSE_INVALID


@pytest.mark.anyio
async def test_oversized_output_is_rejected_without_truncation() -> None:
    limits = RunLimits(output_max_bytes=8, output_max_code_points=8)
    completion = _FakeCompletion(_response(text="0123456789"))
    with pytest.raises(ModelProviderError) as error:
        await NervosChatHandler().run(completion, _running_run(limits), 0)
    assert error.value.code == MODEL_OUTPUT_TOO_LARGE


@pytest.mark.anyio
async def test_multibyte_output_is_bounded_by_bytes_not_only_code_points() -> None:
    limits = RunLimits(output_max_bytes=4, output_max_code_points=1000)
    completion = _FakeCompletion(_response(text="ééé"))
    with pytest.raises(ModelProviderError) as error:
        await NervosChatHandler().run(completion, _running_run(limits), 0)
    assert error.value.code == MODEL_OUTPUT_TOO_LARGE


@pytest.mark.anyio
async def test_provider_identity_mismatch_is_rejected() -> None:
    completion = _FakeCompletion(_response(provider="other-provider"))
    with pytest.raises(ModelProviderError) as error:
        await NervosChatHandler().run(completion, _running_run(), 0)
    assert error.value.code == MODEL_RESPONSE_INVALID


@pytest.mark.anyio
async def test_blank_model_identity_is_rejected() -> None:
    completion = _FakeCompletion(_response(model="   "))
    with pytest.raises(ModelProviderError) as error:
        await NervosChatHandler().run(completion, _running_run(), 0)
    assert error.value.code == MODEL_RESPONSE_INVALID


@pytest.mark.anyio
async def test_non_running_run_is_rejected() -> None:
    created = Run(
        7,
        3,
        "nervos.chat",
        "1",
        "test-provider",
        "opaque/model",
        "hello",
        STAGE_B_LIMITS,
        RunStatus.CREATED,
        CREATED,
    )
    completion = _FakeCompletion(_response())
    with pytest.raises(ModelProviderError):
        await NervosChatHandler().run(completion, created, 0)
    assert completion.requests == []


@pytest.mark.anyio
async def test_more_than_one_allowed_model_call_is_rejected() -> None:
    limits = RunLimits(max_model_calls=2)
    completion = _FakeCompletion(_response())
    with pytest.raises(ModelProviderError):
        await NervosChatHandler().run(completion, _running_run(limits), 0)
    assert completion.requests == []


@pytest.mark.anyio
async def test_provider_failure_propagates_without_a_second_call() -> None:
    completion = _FakeCompletion(error=ModelProviderError(MODEL_OUTPUT_INCOMPLETE))
    with pytest.raises(ModelProviderError) as error:
        await NervosChatHandler().run(completion, _running_run(), 0)
    assert error.value.code == MODEL_OUTPUT_INCOMPLETE
    assert len(completion.requests) == 1


def test_builtin_registry_resolves_exact_version() -> None:
    resolver = create_builtin_handler_registry()
    assert isinstance(resolver.resolve(CHAT_DEFINITION_ID), NervosChatHandler)


def test_builtin_registry_rejects_unknown_version_and_key() -> None:
    resolver = create_builtin_handler_registry()
    for identity in (
        AgentDefinitionId("nervos.chat", "2"),
        AgentDefinitionId("nervos.chat", "10"),
        AgentDefinitionId("nervos.chat", "latest"),
        AgentDefinitionId("other.chat", "1"),
    ):
        with pytest.raises(UnknownAgentHandler):
            resolver.resolve(identity)


def test_registry_rejects_duplicate_exact_identity() -> None:
    handler = NervosChatHandler()
    with pytest.raises(DuplicateAgentHandler):
        BuiltInTrustedAgentHandlerRegistry(
            [(CHAT_DEFINITION_ID, handler), (CHAT_DEFINITION_ID, handler)]
        )


def test_registry_has_no_latest_or_key_only_fallback() -> None:
    resolver = create_builtin_handler_registry()
    assert resolver.resolve(CHAT_DEFINITION_ID) is not None
    with pytest.raises(UnknownAgentHandler):
        resolver.resolve(AgentDefinitionId("nervos.chat", "latest"))
