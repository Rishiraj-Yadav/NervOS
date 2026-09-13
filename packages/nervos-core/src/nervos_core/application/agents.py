"""Agent Instance and Run application services and persistence contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from nervos_core.application.agent_definitions import AgentDefinitionResolver
from nervos_core.application.clock import Clock, require_utc
from nervos_core.application.model_providers import ModelProviderCatalog
from nervos_core.application.trusted_chat import TrustedAgentHandlerResolver
from nervos_core.domain.agents import (
    AgentDefinitionId,
    AgentInstance,
    validate_display_name,
    validate_model_name,
    validate_model_provider,
)
from nervos_core.domain.runs import (
    ModelUsage,
    Run,
    RunLimits,
    validate_error_message,
    validate_input_text,
    validate_outcome_code,
    validate_output_text,
)


class AgentInstanceNotFound(LookupError):
    pass


class AgentInstanceUnavailable(LookupError):
    pass


class RunNotFound(LookupError):
    pass


class RunTransitionRejected(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class InstanceConfiguration:
    display_name: str
    model_provider: str
    model_name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "display_name", validate_display_name(self.display_name))
        object.__setattr__(self, "model_provider", validate_model_provider(self.model_provider))
        object.__setattr__(self, "model_name", validate_model_name(self.model_name))


class AgentPersistence(Protocol):
    def create_instance(
        self,
        owner_user_id: int,
        definition_id: AgentDefinitionId,
        configuration: InstanceConfiguration,
        enabled: bool,
        now: datetime,
    ) -> AgentInstance: ...
    def get_instance(self, owner_user_id: int, instance_id: int) -> AgentInstance: ...
    def list_instances(
        self, owner_user_id: int, limit: int, before_id: int | None
    ) -> tuple[AgentInstance, ...]: ...
    def update_instance(
        self,
        owner_user_id: int,
        instance_id: int,
        configuration: InstanceConfiguration,
        now: datetime,
    ) -> AgentInstance: ...
    def set_instance_enabled(
        self, owner_user_id: int, instance_id: int, enabled: bool, now: datetime
    ) -> AgentInstance: ...
    def create_run_for_owned_instance(
        self,
        owner_user_id: int,
        instance_id: int,
        definition_id: AgentDefinitionId,
        input_text: str,
        limits: RunLimits,
        now: datetime,
    ) -> Run: ...
    def get_run(self, owner_user_id: int, run_id: int) -> Run: ...
    def list_runs(
        self, owner_user_id: int, instance_id: int, limit: int, before_id: int | None
    ) -> tuple[Run, ...]: ...
    def mark_running(self, owner_user_id: int, run_id: int, now: datetime) -> Run: ...
    def mark_succeeded(
        self,
        owner_user_id: int,
        run_id: int,
        output_text: str,
        finish_reason: str | None,
        usage: ModelUsage,
        elapsed_ms: int,
        now: datetime,
    ) -> Run: ...
    def mark_failed(
        self,
        owner_user_id: int,
        run_id: int,
        error_code: str,
        error_message: str,
        usage: ModelUsage,
        elapsed_ms: int,
        now: datetime,
    ) -> Run: ...


class AgentService:
    def __init__(
        self,
        persistence: AgentPersistence,
        definitions: AgentDefinitionResolver,
        clock: Clock,
        handlers: TrustedAgentHandlerResolver | None = None,
        providers: ModelProviderCatalog | None = None,
    ) -> None:
        self._persistence = persistence
        self._definitions = definitions
        self._clock = clock
        self._handlers = handlers
        self._providers = providers

    def create_instance(
        self,
        owner_user_id: int,
        definition_id: AgentDefinitionId,
        display_name: str,
        model_provider: str,
        model_name: str,
        *,
        enabled: bool = True,
    ) -> AgentInstance:
        self._definitions.resolve(definition_id)
        configuration = InstanceConfiguration(
            validate_display_name(display_name),
            validate_model_provider(model_provider),
            validate_model_name(model_name),
        )
        return self._persistence.create_instance(
            owner_user_id, definition_id, configuration, enabled, require_utc(self._clock())
        )

    def update_instance(
        self,
        owner_user_id: int,
        instance_id: int,
        display_name: str,
        model_provider: str,
        model_name: str,
    ) -> AgentInstance:
        configuration = InstanceConfiguration(
            validate_display_name(display_name),
            validate_model_provider(model_provider),
            validate_model_name(model_name),
        )
        return self._persistence.update_instance(
            owner_user_id, instance_id, configuration, require_utc(self._clock())
        )

    def set_enabled(self, owner_user_id: int, instance_id: int, enabled: bool) -> AgentInstance:
        return self._persistence.set_instance_enabled(
            owner_user_id, instance_id, enabled, require_utc(self._clock())
        )

    def create_run(
        self,
        owner_user_id: int,
        instance_id: int,
        input_text: str,
        handlers: TrustedAgentHandlerResolver | None = None,
        providers: ModelProviderCatalog | None = None,
    ) -> Run:
        """Insert one `created` Run snapshot after every known pre-run check passes.

        Handler resolution, provider resolution, and provider configuration availability are
        verified before the insert, so a known pre-run rejection persists no Run at all. The
        insert itself re-checks ownership, enabled state, and exact definition identity
        atomically.
        """
        instance = self._persistence.get_instance(owner_user_id, instance_id)
        definition = self._definitions.resolve(instance.definition_id)
        limits = definition.limits
        resolved_handlers = handlers or self._handlers
        resolved_providers = providers or self._providers
        if resolved_handlers is None or resolved_providers is None:
            raise RuntimeError("agent execution resolvers are not configured")
        resolved_handlers.resolve(instance.definition_id)
        resolved_providers.resolve(instance.model_provider)
        validate_input_text(input_text, limits)
        return self._persistence.create_run_for_owned_instance(
            owner_user_id,
            instance_id,
            instance.definition_id,
            input_text,
            limits,
            require_utc(self._clock()),
        )

    def start(self, owner_user_id: int, run_id: int) -> Run:
        """Atomically transition an owned `created` Run to `running` and return it."""
        return self._persistence.mark_running(owner_user_id, run_id, require_utc(self._clock()))

    def succeed(
        self,
        owner_user_id: int,
        run_id: int,
        output_text: str,
        finish_reason: str | None,
        usage: ModelUsage,
        elapsed_ms: int,
    ) -> Run:
        run = self._persistence.get_run(owner_user_id, run_id)
        validate_output_text(output_text, run.limits)
        if finish_reason is not None:
            validate_outcome_code(finish_reason)
        return self._persistence.mark_succeeded(
            owner_user_id,
            run_id,
            output_text,
            finish_reason,
            usage,
            elapsed_ms,
            require_utc(self._clock()),
        )

    def fail(
        self,
        owner_user_id: int,
        run_id: int,
        error_code: str,
        error_message: str,
        usage: ModelUsage,
        elapsed_ms: int,
    ) -> Run:
        validate_outcome_code(error_code)
        validate_error_message(error_message)
        return self._persistence.mark_failed(
            owner_user_id,
            run_id,
            error_code,
            error_message,
            usage,
            elapsed_ms,
            require_utc(self._clock()),
        )
