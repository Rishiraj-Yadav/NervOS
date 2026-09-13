"""B1 domain, registry, and model-port contracts."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from nervos_core.application.agent_definitions import (
    BuiltInAgentDefinitionRegistry,
    DuplicateAgentDefinition,
    UnknownAgentDefinition,
    create_builtin_definition_registry,
)
from nervos_core.application.model_completion import ModelCompletion, ModelRequest, ModelResponse
from nervos_core.domain.agents import (
    AgentDefinition,
    AgentDefinitionId,
    InvalidAgentInstance,
    validate_display_name,
    validate_model_name,
)
from nervos_core.domain.runs import (
    STAGE_B_LIMITS,
    InvalidRun,
    ModelUsage,
    Run,
    RunLimits,
    RunStatus,
    is_blank_text,
    validate_input_text,
    validate_output_text,
)

BLANK_TEXTS = (
    " ",
    chr(9),
    chr(10),
    chr(13) + chr(10),
    " " + chr(9) + chr(13) + chr(10) + " ",
    chr(0x00A0) + chr(0x2003),
    chr(0x2028),
    chr(0x3000),
)
MEANINGFUL_TEXTS = (
    chr(9) + "hello",
    "hello" + chr(10),
    chr(10) + "hello" + chr(10),
    "    code    ",
    "code" + chr(0x2003) + "block",
)


def test_blank_text_policy_is_fixed_and_preserves_meaningful_whitespace() -> None:
    for value in ("", *BLANK_TEXTS):
        assert is_blank_text(value)
        with pytest.raises(InvalidRun):
            validate_input_text(value)
        with pytest.raises(InvalidRun):
            validate_output_text(value, STAGE_B_LIMITS)
    for value in MEANINGFUL_TEXTS:
        assert not is_blank_text(value)
        assert validate_input_text(value) == value
        assert validate_output_text(value, STAGE_B_LIMITS) == value


def test_builtin_definition_requires_exact_version_and_has_b0_limits() -> None:
    registry = create_builtin_definition_registry()
    definition = registry.resolve(AgentDefinitionId("nervos.chat", "1"))
    assert definition.limits == STAGE_B_LIMITS
    assert STAGE_B_LIMITS.values() == (8000, 4000, 32000, 16000, 60000, 1024, 1)
    with pytest.raises(UnknownAgentDefinition):
        registry.resolve(AgentDefinitionId("nervos.chat", "2"))


def test_duplicate_definition_is_rejected() -> None:
    definition = AgentDefinition(AgentDefinitionId("nervos.chat", "1"), "Chat", STAGE_B_LIMITS)
    with pytest.raises(DuplicateAgentDefinition):
        BuiltInAgentDefinitionRegistry((definition, definition))


def test_identifier_and_display_validation_preserves_opaque_values() -> None:
    assert validate_display_name("  Research  ") == "Research"
    assert validate_model_name("  Org/Model:v1.2-β  ") == "Org/Model:v1.2-β"
    with pytest.raises(InvalidAgentInstance):
        validate_display_name("bad\nname")
    with pytest.raises(InvalidAgentInstance):
        validate_model_name("bad\x00model")


def test_multiline_input_and_output_are_preserved() -> None:
    prompt = "Write code:\r\n\tprint('ok')"
    output = "Result:\n```python\nprint('ok')\n```"
    assert validate_input_text(prompt) == prompt
    assert validate_output_text(output, STAGE_B_LIMITS) == output
    for invalid in ("", " \t\r\n", "bad\x00text"):
        with pytest.raises(InvalidRun):
            validate_input_text(invalid)


def test_limits_are_structurally_positive_not_fixed_to_b0() -> None:
    future = RunLimits(16000, 8000, 64000, 32000, 120000, 2048, 2)
    assert future.max_model_calls == 2
    with pytest.raises(InvalidRun):
        RunLimits(max_model_calls=0)


def test_usage_is_independently_nullable_and_nonnegative() -> None:
    assert ModelUsage(input_tokens=1).total_tokens is None
    with pytest.raises(InvalidRun):
        ModelUsage(output_tokens=-1)


def test_complete_run_shapes_are_enforced() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)

    def make_run(status: RunStatus, **outcome: object) -> Run:
        return Run(
            id=1,
            agent_instance_id=1,
            agent_key="nervos.chat",
            agent_definition_version="1",
            model_provider="test-provider",
            model_name="x/y",
            input_text="hello",
            limits=STAGE_B_LIMITS,
            status=status,
            created_at=now,
            **outcome,  # type: ignore[arg-type]
        )

    created = make_run(RunStatus.CREATED)
    assert created.status is RunStatus.CREATED
    running = make_run(RunStatus.RUNNING, started_at=now)
    assert running.status is RunStatus.RUNNING
    succeeded = make_run(
        RunStatus.SUCCEEDED,
        started_at=now,
        finished_at=now,
        output_text="ok",
        elapsed_ms=0,
    )
    assert succeeded.output_text == "ok"
    failed = make_run(
        RunStatus.FAILED,
        started_at=now,
        finished_at=now,
        error_code="model_error",
        error_message="Safe failure",
        elapsed_ms=0,
    )
    assert failed.error_code == "model_error"
    for field, value in (
        ("output_text", ""),
        ("finish_reason", ""),
        ("error_code", ""),
        ("error_message", ""),
        ("elapsed_ms", 0),
    ):
        with pytest.raises(InvalidRun):
            make_run(RunStatus.CREATED, **{field: value})
        with pytest.raises(InvalidRun):
            make_run(RunStatus.RUNNING, started_at=now, **{field: value})


def test_domain_timestamps_are_normalized_to_utc() -> None:
    offset = timezone(timedelta(hours=5, minutes=30))
    instant = datetime(2026, 1, 1, 5, 30, tzinfo=offset)
    run = Run(
        1,
        1,
        "nervos.chat",
        "1",
        "test-provider",
        "model",
        "hello",
        STAGE_B_LIMITS,
        RunStatus.CREATED,
        instant,
    )
    assert run.created_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert run.created_at.tzinfo is UTC


class _FakeCompletion:
    async def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse("fixed", "test-provider", request.model_name)


def test_model_completion_contract_is_narrow_and_fake_is_test_only() -> None:
    fake: ModelCompletion = _FakeCompletion()
    request = ModelRequest("system", "user", "opaque/model", 1024, 60000)
    assert fake is not None
    assert not hasattr(request, "messages")
