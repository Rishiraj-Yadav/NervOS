"""Pydantic schemas exposed by the NervOS API."""

from datetime import datetime
from typing import Annotated, Literal

from nervos_core.application.mcp_connections import McpConnectionRow
from nervos_core.domain.agents import AgentInstance, InvalidAgentInstance
from nervos_core.domain.jobs import JobStatus, RunEvent, RunEventType
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


class RunEventResponse(BaseModel):
    """One durable Run Event, as an explicit allow-list of safe facts.

    This is an allow-list rather than a serialization of the stored row. The durable Event carries
    only fields that are safe to publish: it has no column for a claim token, lease, worker
    identity, heartbeat, provider payload, prompt, output, or environment value, so nothing here
    could leak even by accident. The four identifiers it *does* store -- the global event `id`, and
    the `run_id`/`job_id`/`attempt_id` of the execution-obligation graph -- are deliberately not
    projected: they are internal identity, and `sequence` plus `attempt_number` already carry every
    fact the timeline needs. `tool_invocation_id` is the one stored identifier that *is* projected:
    a tool event's timeline row needs an opaque handle back to its invocation, and the handle names
    no tool, argument, result, digest, or connection.
    """

    sequence: int
    event_type: RunEventType
    created_at: datetime
    attempt_number: int | None
    code: str | None
    message: str | None
    available_at: datetime | None
    tool_invocation_id: int | None = None

    @classmethod
    def from_domain(cls, event: RunEvent) -> "RunEventResponse":
        return cls(
            sequence=event.sequence,
            event_type=event.event_type,
            created_at=event.created_at,
            attempt_number=event.attempt_number,
            code=event.code,
            message=event.message,
            available_at=event.available_at,
            tool_invocation_id=event.tool_invocation_id,
        )


class RunEventPageResponse(BaseModel):
    """One ascending-sequence page of a Run's Event timeline.

    `next_after_sequence` is a keyset cursor, not an offset: it is the last sequence returned when
    the page came back full, and `None` once the history is drained. A client loop is therefore
    `after_sequence = next ?? last_seen`, ending when this is null. A full final page costs one
    further empty request to prove it is finished, which is why no count query exists to avoid it.
    """

    items: list[RunEventResponse]
    next_after_sequence: int | None


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
    # Derived per read from the Run's one durable Job and never persisted, so these cannot drift
    # from what they describe. They are convenience, not authority: nothing mutates on them, and
    # a `running` Run waiting on a retry is otherwise indistinguishable from one executing now.
    execution_phase: JobStatus | None
    retry_available_at: datetime | None

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
            execution_phase=run.execution_phase,
            retry_available_at=run.retry_available_at,
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


class McpConnectionCreateRequest(BaseModel):
    """Configuration of one approved MCP tool source.

    Every field here is a *name*: an opaque operator-declared server key, an opaque credential
    alias, a URL. There is deliberately no field for a command, an argument vector, a shell, a
    working directory, an environment mapping, or the name of an environment variable, and
    ``extra="forbid"`` is what makes that structural -- a request carrying one is rejected rather
    than silently ignored.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: Annotated[str, Field(min_length=1, max_length=100)]
    transport: Literal["http", "stdio"]
    endpoint: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    server_key: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    credential_ref: Annotated[str, Field(min_length=1, max_length=128)] | None = None

    @model_validator(mode="after")
    def _exactly_one_target(self) -> "McpConnectionCreateRequest":
        """Require the one target this transport uses, and forbid the other."""
        if self.transport == "http":
            if self.endpoint is None or self.server_key is not None:
                raise ValueError("an http connection requires exactly one endpoint")
        elif self.server_key is None or self.endpoint is not None:
            raise ValueError("a stdio connection requires exactly one server key")
        return self


class McpConnectionDisplayNameRequest(BaseModel):
    """The one in-place mutation a connection permits."""

    model_config = ConfigDict(extra="forbid")

    display_name: Annotated[str, Field(min_length=1, max_length=100)]


class McpConnectionResponse(BaseModel):
    """Safe persisted connection representation.

    It exposes the credential *alias* -- which is an operator-visible name the owner themselves
    supplied -- and never a credential value, an environment-variable name, an executable, an
    argument vector, or any raw remote text.
    """

    id: int
    display_name: str
    transport: str
    endpoint: str | None
    server_key: str | None
    credential_ref: str | None
    enabled: bool
    catalog_status: str
    last_discovery_at: datetime | None
    last_error_code: str | None
    last_error_message: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, connection: McpConnectionRow) -> "McpConnectionResponse":
        return cls(
            id=connection.connection_id,
            display_name=connection.display_name,
            transport=connection.transport.value,
            endpoint=connection.endpoint,
            server_key=connection.server_key,
            credential_ref=connection.credential_ref,
            enabled=connection.enabled,
            catalog_status=connection.catalog_status.value,
            last_discovery_at=connection.last_discovery_at,
            last_error_code=connection.last_error_code,
            last_error_message=connection.last_error_message,
            created_at=connection.created_at,
            updated_at=connection.updated_at,
        )


class McpConnectionPageResponse(BaseModel):
    """One newest-first page of owned MCP connections."""

    items: list[McpConnectionResponse]
    next_before_id: int | None


class McpConnectionDeletedResponse(BaseModel):
    """Confirmation that a connection and its private definitions were removed."""

    deleted: Literal[True] = True
