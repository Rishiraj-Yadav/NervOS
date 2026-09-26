"""G1 package-aware Agent Definition resolver contracts."""

import pytest
from nervos_core.application.agent_definitions import (
    CompositeAgentDefinitionResolver,
    DuplicateAgentDefinition,
    StaticPackageDefinitionSource,
    UnknownAgentDefinition,
    create_composite_agent_definition_resolver,
)
from nervos_core.domain.agents import AgentDefinition, AgentDefinitionId
from nervos_core.domain.runs import STAGE_B_LIMITS, TOOL_ENABLED_LIMITS


def _definition(agent_key: str, version: str, *, name: str = "Package Agent") -> AgentDefinition:
    return AgentDefinition(
        identity=AgentDefinitionId(agent_key, version),
        display_name=name,
        limits=STAGE_B_LIMITS,
    )


def test_composite_resolver_preserves_existing_builtin_exact_resolution() -> None:
    resolver = create_composite_agent_definition_resolver()

    tool_free = resolver.resolve(AgentDefinitionId("nervos.chat", "1"))
    tool_enabled = resolver.resolve(AgentDefinitionId("nervos.chat", "2"))

    assert tool_free.display_name == "NervOS Chat"
    assert tool_free.limits == STAGE_B_LIMITS
    assert tool_enabled.display_name == "NervOS Chat (tools)"
    assert tool_enabled.limits == TOOL_ENABLED_LIMITS


def test_composite_resolver_resolves_package_definitions_by_exact_identity() -> None:
    package_definition = _definition("example.agent", "1.0.0")
    resolver = create_composite_agent_definition_resolver(
        (StaticPackageDefinitionSource((package_definition,)),)
    )

    assert resolver.resolve(AgentDefinitionId("example.agent", "1.0.0")) is package_definition
    with pytest.raises(UnknownAgentDefinition):
        resolver.resolve(AgentDefinitionId("example.agent", "1.0.1"))
    with pytest.raises(UnknownAgentDefinition):
        resolver.resolve(AgentDefinitionId("example.other", "1.0.0"))


def test_static_package_source_rejects_duplicate_exact_identities() -> None:
    first = _definition("example.agent", "1.0.0", name="First")
    second = _definition("example.agent", "1.0.0", name="Second")

    with pytest.raises(DuplicateAgentDefinition):
        StaticPackageDefinitionSource((first, second))


def test_static_package_source_rejects_reserved_nervos_namespace() -> None:
    with pytest.raises(DuplicateAgentDefinition):
        StaticPackageDefinitionSource((_definition("nervos.package", "1.0.0"),))


def test_composite_resolver_fails_deterministically_on_package_duplicate() -> None:
    first = StaticPackageDefinitionSource((_definition("example.agent", "1.0.0", name="First"),))
    second = StaticPackageDefinitionSource((_definition("example.agent", "1.0.0", name="Second"),))

    with pytest.raises(DuplicateAgentDefinition):
        create_composite_agent_definition_resolver((first, second))


def test_composite_resolver_rejects_package_source_that_collides_with_builtin() -> None:
    class CollidingPackageSource:
        def list_definitions(self) -> tuple[AgentDefinition]:
            return (_definition("example.builtin", "1"),)

    with pytest.raises(DuplicateAgentDefinition):
        CompositeAgentDefinitionResolver(
            builtins=StaticPackageDefinitionSource((_definition("example.builtin", "1"),)),
            package_sources=(CollidingPackageSource(),),
        )


def test_composite_resolver_rejects_g3_package_source_in_reserved_nervos_namespace() -> None:
    class ReservedPackageSource:
        def list_definitions(self) -> tuple[AgentDefinition]:
            return (_definition("nervos.chat", "1"),)

    with pytest.raises(DuplicateAgentDefinition):
        CompositeAgentDefinitionResolver(
            builtins=StaticPackageDefinitionSource((_definition("example.builtin", "1"),)),
            package_sources=(ReservedPackageSource(),),
        )


def test_composite_resolver_accepts_g3_compatible_source_interface() -> None:
    package_definition = _definition("vendor.agent", "2026.09.26")

    class InstalledPackageDefinitionSource:
        def list_definitions(self) -> tuple[AgentDefinition]:
            return (package_definition,)

    resolver = create_composite_agent_definition_resolver((InstalledPackageDefinitionSource(),))

    assert resolver.resolve(package_definition.identity) is package_definition
