"""Exact-version trusted Agent Definition resolution."""

from collections.abc import Iterable
from typing import Protocol

from nervos_core.domain.agents import AgentDefinition, AgentDefinitionId
from nervos_core.domain.runs import STAGE_B_LIMITS, TOOL_ENABLED_LIMITS


class DuplicateAgentDefinition(ValueError):
    """Raised when one exact definition pair is registered twice."""


class UnknownAgentDefinition(LookupError):
    """Raised when an exact definition pair is unavailable."""


class AgentDefinitionResolver(Protocol):
    def resolve(self, definition_id: AgentDefinitionId) -> AgentDefinition: ...


class AgentDefinitionSource(Protocol):
    """G3-compatible source of exact-version Agent Definitions."""

    def list_definitions(self) -> Iterable[AgentDefinition]: ...


class BuiltInAgentDefinitionRegistry:
    """Immutable exact-key registry for trusted built-ins."""

    def __init__(self, definitions: Iterable[AgentDefinition]) -> None:
        entries: dict[AgentDefinitionId, AgentDefinition] = {}
        for definition in definitions:
            if definition.identity in entries:
                raise DuplicateAgentDefinition
            entries[definition.identity] = definition
        self._entries = entries

    def list_definitions(self) -> Iterable[AgentDefinition]:
        return tuple(self._entries.values())

    def resolve(self, definition_id: AgentDefinitionId) -> AgentDefinition:
        try:
            return self._entries[definition_id]
        except KeyError as error:
            raise UnknownAgentDefinition from error


class StaticPackageDefinitionSource:
    """Deterministic in-memory package definition source for G1 tests and later G3 adapters."""

    def __init__(self, definitions: Iterable[AgentDefinition]) -> None:
        entries: dict[AgentDefinitionId, AgentDefinition] = {}
        for definition in definitions:
            if definition.identity.agent_key.startswith("nervos."):
                raise DuplicateAgentDefinition
            if definition.identity in entries:
                raise DuplicateAgentDefinition
            entries[definition.identity] = definition
        self._entries = entries

    def list_definitions(self) -> Iterable[AgentDefinition]:
        return tuple(self._entries.values())


class CompositeAgentDefinitionResolver:
    """Exact resolver over built-in definitions plus package definition sources."""

    def __init__(
        self,
        builtins: AgentDefinitionSource,
        package_sources: Iterable[AgentDefinitionSource] = (),
    ) -> None:
        entries: dict[AgentDefinitionId, AgentDefinition] = {}
        builtin_identities: set[AgentDefinitionId] = set()
        for definition in builtins.list_definitions():
            if definition.identity in entries:
                raise DuplicateAgentDefinition
            entries[definition.identity] = definition
            builtin_identities.add(definition.identity)

        for source in package_sources:
            for definition in source.list_definitions():
                if definition.identity.agent_key.startswith("nervos."):
                    raise DuplicateAgentDefinition
                if definition.identity in builtin_identities or definition.identity in entries:
                    raise DuplicateAgentDefinition
                entries[definition.identity] = definition

        self._entries = entries

    def resolve(self, definition_id: AgentDefinitionId) -> AgentDefinition:
        try:
            return self._entries[definition_id]
        except KeyError as error:
            raise UnknownAgentDefinition from error


def create_builtin_definition_registry() -> BuiltInAgentDefinitionRegistry:
    """Return the immutable set of trusted built-in definitions.

    Two exact identities, never a version range. `nervos.chat@1` remains the tool-free one-call
    agent it has always been, with unchanged limits; `nervos.chat@2` is the tool-enabled agent
    registered by D4. Resolution stays exact, so a later version can never become reachable here
    merely because it was registered.
    """
    return BuiltInAgentDefinitionRegistry(
        (
            AgentDefinition(
                identity=AgentDefinitionId("nervos.chat", "1"),
                display_name="NervOS Chat",
                limits=STAGE_B_LIMITS,
            ),
            AgentDefinition(
                identity=AgentDefinitionId("nervos.chat", "2"),
                display_name="NervOS Chat (tools)",
                limits=TOOL_ENABLED_LIMITS,
            ),
        )
    )


def create_composite_agent_definition_resolver(
    package_sources: Iterable[AgentDefinitionSource] = (),
) -> AgentDefinitionResolver:
    """Compose trusted built-ins with package definition sources using exact resolution."""
    return CompositeAgentDefinitionResolver(create_builtin_definition_registry(), package_sources)
