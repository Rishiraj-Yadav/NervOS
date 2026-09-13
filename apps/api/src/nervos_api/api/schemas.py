"""Pydantic schemas exposed by the NervOS API."""

from datetime import datetime
from typing import Annotated, Literal

from nervos_core.domain.agents import AgentInstance, InvalidAgentInstance
from nervos_core.domain.runs import ModelUsage, Run, RunStatus
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class HealthResponse(BaseModel):
    """Public liveness response."""

    status: Literal["ok"] = "ok"


class CredentialRequest(BaseModel):
    """Credential input whose representation never exposes its password."""

    model_config = ConfigDict(extra="forbid")

    username: Annotated[str, Field(min_length=1, max_length=128)]
    password: Annotated[SecretStr, Field(min_length=12, max_length=128)]


class SetupStatusResponse(BaseModel):
    """Public first-run setup availability state."""

    setup_complete: bool


class UserResponse(BaseModel):
    """Safe authenticated-user response."""

    id: int
    username: str
    role: str
    is_active: bool


class ErrorDetail(BaseModel):
    """Stable public error information."""

    code: str
    message: str


class ErrorResponse(BaseModel):
    """Stable error envelope."""

    error: ErrorDetail


class AgentInstanceCreateRequest(BaseModel):
    """Explicit creation of one owned Agent Instance.

    The exact trusted definition identity is always client-supplied and explicit; the route
    additionally restricts it to the single definition this milestone exposes.
    """

    model_config = ConfigDict(extra="forbid")

    agent_key: str
    agent_definition_version: str
    display_name: str
    model_provider: str
    model_name: str
    enabled: bool = True


class AgentInstanceUpdateRequest(BaseModel):
    """Exactly one of two shapes: the configuration triple, or `enabled` alone.

    Partial configuration and any combination of `enabled` with configuration fields are
    rejected, so every accepted request maps to exactly one committed instance mutation.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = None
    model_provider: str | None = None
    model_name: str | None = None
    enabled: bool | None = None

    @model_validator(mode="after")
    def _exactly_one_shape(self) -> "AgentInstanceUpdateRequest":
        configuration = (self.display_name, self.model_provider, self.model_name)
        supplied = [value is not None for value in configuration]

        if any(supplied) and not all(supplied):
            raise ValueError(
                "display_name, model_provider and model_name must be supplied together"
            )
        if any(supplied) and self.enabled is not None:
            raise ValueError("enabled cannot be combined with configuration fields")
        if not any(supplied) and self.enabled is None:
            raise ValueError("a patch must supply either the configuration triple or enabled")
        return self

    def configuration_triple(self) -> tuple[str, str, str]:
        """Return the shape-A configuration values exactly as supplied."""
        display_name, model_provider, model_name = (
            self.display_name,
            self.model_provider,
            self.model_name,
        )
        if display_name is None or model_provider is None or model_name is None:
            # Unreachable: the validator above rejects any partial configuration.
            raise InvalidAgentInstance("incomplete agent instance configuration")
        return display_name, model_provider, model_name

    def enabled_toggle(self) -> bool:
        """Return the shape-B enable-state value."""
        if self.enabled is None:
            # Unreachable: the validator above rejects an empty update.
            raise InvalidAgentInstance("missing agent instance enable state")
        return self.enabled


class AgentInstanceResponse(BaseModel):
    """Safe persisted Agent Instance representation that never exposes its owner."""

    id: int
    agent_key: str
    agent_definition_version: str
    display_name: str
    enabled: bool
    model_provider: str
    model_name: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, instance: AgentInstance) -> "AgentInstanceResponse":
        return cls(
            id=instance.id,
            agent_key=instance.definition_id.agent_key,
            agent_definition_version=instance.definition_id.agent_definition_version,
            display_name=instance.display_name,
            enabled=instance.enabled,
            model_provider=instance.model_provider,
            model_name=instance.model_name,
            created_at=instance.created_at,
            updated_at=instance.updated_at,
        )


class AgentInstancePageResponse(BaseModel):
    """One newest-first page of owned Agent Instances."""

    items: list[AgentInstanceResponse]
    next_before_id: int | None


class RunCreateRequest(BaseModel):
    """One bounded user submission carrying no history and no conversation identity."""

    model_config = ConfigDict(extra="forbid")

    input: str


class RunUsageResponse(BaseModel):
    """Trustworthy reported usage only; never estimated, derived, or completed."""

    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None


class RunResponse(BaseModel):
    """Immutable persisted Run representation with no limits or provider internals."""

    id: int
    agent_instance_id: int
    agent_key: str
    agent_definition_version: str
    model_provider: str
    model_name: str
    input_text: str
    status: RunStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    output_text: str | None
    finish_reason: str | None
    error_code: str | None
    error_message: str | None
    usage: RunUsageResponse | None
    elapsed_ms: int | None

    @classmethod
    def from_domain(cls, run: Run) -> "RunResponse":
        return cls(
            id=run.id,
            agent_instance_id=run.agent_instance_id,
            agent_key=run.agent_key,
            agent_definition_version=run.agent_definition_version,
            model_provider=run.model_provider,
            model_name=run.model_name,
            input_text=run.input_text,
            status=run.status,
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            output_text=run.output_text,
            finish_reason=run.finish_reason,
            error_code=run.error_code,
            error_message=run.error_message,
            usage=usage_response(run.usage),
            elapsed_ms=run.elapsed_ms,
        )


class RunPageResponse(BaseModel):
    """One newest-first page of owned Runs."""

    items: list[RunResponse]
    next_before_id: int | None


def usage_response(usage: ModelUsage) -> RunUsageResponse | None:
    """Return safe usage, or null rather than a meaningless all-null usage object."""
    if usage.values() == (None, None, None):
        return None
    return RunUsageResponse(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
    )
