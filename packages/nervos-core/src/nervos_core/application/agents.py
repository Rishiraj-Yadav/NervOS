"""Agent Instance and Run application services and persistence contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from nervos_core.application.agent_definitions import AgentDefinitionResolver
from nervos_core.application.clock import Clock, require_utc
from nervos_core.application.model_providers import ModelProviderCatalog, UnknownModelProvider
from nervos_core.domain.agents import (
    AgentDefinitionId,
    AgentInstance,
    validate_display_name,
    validate_model_name,
    validate_model_provider,
)
from nervos_core.domain.runs import (
    Run,
    RunLimits,
    validate_input_text,
)


class AgentInstanceNotFound(LookupError):
    pass


class AgentInstanceUnavailable(LookupError):
    pass


class RunNotFound(LookupError):
    pass


class DurableSubmissionRejected(ValueError):
    """The owned enabled Agent Instance was unavailable at commit time.

    The durable submission primitive re-checks ownership, enabled state, and exact definition
    identity inside its own transaction, so this is the one race that a pre-flight read cannot
    close. The application layer translates it into the same owner-visible failure a
    pre-flight read would have produced.
    """


class RunSubmissionPersistence(Protocol):
    """Durable acceptance of one Run: Run + Job + initial events, atomically."""

    def submit(
        self,
        *,
        owner_user_id: int,
        agent_instance_id: int,
        input_text: str,
        limits: RunLimits,
        definition_id: AgentDefinitionId | None = None,
        now: datetime,
        max_attempts: int = 3,
        max_pending: int | None = None,
    ) -> Run: ...


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
    def get_run(self, owner_user_id: int, run_id: int) -> Run: ...
    def list_runs(
        self, owner_user_id: int, instance_id: int, limit: int, before_id: int | None
    ) -> tuple[Run, ...]: ...


class AgentService:
    """Own Agent Instances and Run history, and durably accept new Runs.

    The service holds no handler registry and no executor: it validates structure and commits
    the durable obligation, and the execution plane is what runs it.
    """

    def __init__(
        self,
        persistence: AgentPersistence,
        definitions: AgentDefinitionResolver,
        clock: Clock,
        providers: ModelProviderCatalog | None = None,
        submissions: RunSubmissionPersistence | None = None,
    ) -> None:
        self._persistence = persistence
        self._definitions = definitions
        self._clock = clock
        self._providers = providers
        self._submissions = submissions

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

    def get_instance(self, owner_user_id: int, instance_id: int) -> AgentInstance:
        """Return one owned Agent Instance, or raise for a nonexistent or foreign id."""
        return self._persistence.get_instance(owner_user_id, instance_id)

    def list_instances(
        self, owner_user_id: int, limit: int, before_id: int | None
    ) -> tuple[AgentInstance, ...]:
        """Return one owner-scoped page of newest-first Agent Instances."""
        return self._persistence.list_instances(owner_user_id, limit, before_id)

    def get_run(self, owner_user_id: int, run_id: int) -> Run:
        """Return one owned Run, or raise for a nonexistent or foreign id."""
        return self._persistence.get_run(owner_user_id, run_id)

    def list_runs(
        self, owner_user_id: int, instance_id: int, limit: int, before_id: int | None
    ) -> tuple[Run, ...]:
        """Return one owner-scoped page of newest-first Runs for an owned Agent Instance.

        The owner-scoped parent is resolved before the Runs are queried, so a nonexistent or
        foreign Agent Instance is reported as not found rather than as an empty history. The Run
        query alone cannot tell those apart: it is scoped by the same ownership predicate, so it
        returns no rows for an instance that exists with no Runs and for one that does not exist
        at all. Resolving the parent first is what keeps "this instance has no runs yet" distinct
        from "this instance is not yours" — and the two failures stay indistinguishable because a
        single owner-scoped lookup produces both.
        """
        self._persistence.get_instance(owner_user_id, instance_id)
        return self._persistence.list_runs(owner_user_id, instance_id, limit, before_id)

    def submit_run(self, owner_user_id: int, instance_id: int, input_text: str) -> Run:
        """Durably accept one Run without executing anything.

        The control plane validates structure only: the owned Agent Instance is resolved, the
        exact definition identity is pinned, the provider identifier must be one NervOS
        knows, and the input must satisfy the definition's limits. Whether *this* process
        holds a credential is deliberately not part of admission, because execution is a
        Worker capability; a known but locally unconfigured provider is accepted and waits
        in the queue until a capable Worker exists.

        The Run, its Job, and both initial Run Events commit together inside one
        `BEGIN IMMEDIATE` transaction, so a rejected submission leaves no partial state and
        consumes no identifier.
        """
        instance = self._persistence.get_instance(owner_user_id, instance_id)
        definition = self._definitions.resolve(instance.definition_id)
        if self._providers is None:
            raise RuntimeError("agent execution resolvers are not configured")
        if not self._providers.is_known(instance.model_provider):
            raise UnknownModelProvider(instance.model_provider)
        validate_input_text(input_text, definition.limits)
        if self._submissions is None:
            raise RuntimeError("durable submission is not configured")
        try:
            return self._submissions.submit(
                owner_user_id=owner_user_id,
                agent_instance_id=instance_id,
                input_text=input_text,
                limits=definition.limits,
                definition_id=instance.definition_id,
                now=require_utc(self._clock()),
            )
        except DurableSubmissionRejected as error:
            raise AgentInstanceUnavailable from error
