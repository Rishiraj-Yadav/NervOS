"""Per-Attempt tool catalog assembly: what this Run may offer the model, and nothing more.

ADR 0015 fixes the catalog's lifetime and its two jobs: it is **grant-filtered** and it is **frozen
for one Attempt**. The catalog bounds what the model is *offered*; the permission predicate bounds
what is actually *done*, live, immediately before each dispatch. Neither substitutes for the other,
which is why removing an entry here can never authorize anything and re-checking there is never
skipped because an entry exists.

**Membership grants nothing.** Every candidate is submitted to the live D2 evaluator before it is
presented, and an entry that survives is still re-checked at call time.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from nervos_core.application.model_completion import ToolSchema
from nervos_core.application.tool_permissions import ToolPermissionEvaluator
from nervos_core.application.tool_registry import ToolRegistry
from nervos_core.application.tool_schema import (
    CanonicalSchema,
    SchemaRejection,
    validate_canonical_schema,
)
from nervos_core.domain.runs import Run
from nervos_core.domain.tools import JsonValue, ToolDescriptor, canonical_json_text

# The frozen provider-neutral catalog bounds. Both are counted over the *whole* ordered array as it
# would be presented, so a catalog that fits is one a provider can actually be handed.
CATALOG_MAX_TOOLS = 32
CATALOG_MAX_BYTES = 65_536


class ToolCatalogInvalid(Exception):
    """Raised when the assembled catalog cannot be presented within the frozen bounds.

    This is a configuration failure, never a reason to truncate: dropping entries or shortening
    schemas to fit would silently change which tools an agent has, which is a security-relevant
    fact rather than a formatting one.
    """


@dataclass(frozen=True, slots=True)
class ToolCatalogEntry:
    """One offered tool: its durable descriptor, its neutral schema, and its parsed input schema."""

    descriptor: ToolDescriptor
    schema: ToolSchema
    canonical_input_schema: CanonicalSchema

    @property
    def model_name(self) -> str:
        return self.descriptor.model_name


@dataclass(frozen=True, slots=True)
class ToolCatalog:
    """The immutable tool catalog for one Attempt."""

    entries: tuple[ToolCatalogEntry, ...]
    byte_size: int
    _by_model_name: Mapping[str, ToolCatalogEntry]

    def lookup(self, model_name: str) -> ToolCatalogEntry | None:
        """Return the entry the model named, or ``None`` if it named nothing offered.

        A name that is not in the catalog is not an error in the provider's response -- a model may
        legitimately produce one -- so this returns absence rather than raising.
        """
        return self._by_model_name.get(model_name)

    def schemas(self) -> tuple[ToolSchema, ...]:
        return tuple(entry.schema for entry in self.entries)

    def __len__(self) -> int:
        return len(self.entries)


def _canonical_input_schema(descriptor: ToolDescriptor) -> CanonicalSchema:
    """Parse and validate one descriptor's input schema once, for the whole Attempt.

    A descriptor's schema was already proved canonical when the definition was persisted, so a
    rejection here means the two disagree -- a defect in our own path rather than a configuration
    problem. It therefore raises rather than becoming a catalog verdict, and it is raised as an
    invalid catalog so the Run fails closed before a provider is called.
    """
    schema = validate_canonical_schema(descriptor.input_schema)
    if isinstance(schema, SchemaRejection):  # pragma: no cover - registration already proves this
        raise ToolCatalogInvalid("a descriptor carries a non-canonical input schema")
    return schema


class ToolSourceSynchronizer(Protocol):
    """Make the sources a Run's catalog will be gathered from available in this process.

    A Worker's registry is process-local state, but a connection is durable and may have been
    created, refreshed or disabled by the control plane, or by a different Worker, at any moment.
    Without this seam the registry would only ever reflect what existed when the process started, so
    a tool discovered a second later would stay invisible until a restart.

    Implementations read durable state only. Synchronising is **not** authorising: it may register a
    source, and D2's live evaluator still decides every call, so doing nothing here can only ever
    withhold a tool -- never permit one. It must also perform no remote I/O, because it runs on the
    path to assembling a catalog and a network round trip per Attempt would make the catalog's
    contents depend on a remote server being reachable.
    """

    async def synchronize_for_run(self, run: Run) -> None: ...


async def gather_descriptors(registry: ToolRegistry) -> tuple[ToolDescriptor, ...]:
    """Collect every descriptor the registry can currently offer, across all registered sources.

    One Attempt's catalog is a view over *all* the sources this process has registered -- the
    built-ins and each MCP connection alike -- rather than over a single named source. That is what
    lets a Run hold grants from a built-in and from two different connections at once, and it is why
    nothing here restricts a Run to one MCP connection or invents an aggregate source identity: the
    registry is already keyed by real :class:`ToolSourceRef`, so iterating it needs no new concept.

    Gathering is not authorisation. Every descriptor collected here is still submitted to the live
    permission evaluator inside :func:`assemble_tool_catalog`, and an entry that survives is still
    re-checked immediately before it is dispatched. A definition whose connection is disabled or
    whose fingerprint has drifted is therefore dropped by the evaluator, not by this function.

    No de-duplication happens here, deliberately. A definition belongs to exactly one source, so two
    registered sources cannot report the same one; collapsing rows by id would be solving a problem
    that cannot occur, and it would silently drop genuinely distinct candidates that happen to share
    an id in a test fixture.
    """
    descriptors: list[ToolDescriptor] = []
    for source_ref in registry.source_refs():
        descriptors.extend(await registry.source(source_ref).list_tools())
    return tuple(descriptors)


def assemble_tool_catalog(
    *,
    descriptors: Sequence[ToolDescriptor],
    run_id: int,
    authorize: ToolPermissionEvaluator,
) -> ToolCatalog:
    """Filter, order, and measure the catalog for one Attempt.

    Ordering is fixed **before** measurement, so the same set of tools always produces the same
    bytes and a catalog cannot pass or fail its bound by accident of iteration order.
    """
    permitted = [
        descriptor
        for descriptor in descriptors
        if authorize.check_permission(
            run_id=run_id, tool_definition_id=descriptor.tool_definition_id
        ).allowed
    ]
    permitted.sort(key=lambda descriptor: descriptor.model_name)
    if len(permitted) > CATALOG_MAX_TOOLS:
        raise ToolCatalogInvalid("too many tools")
    entries = tuple(
        ToolCatalogEntry(
            descriptor=descriptor,
            schema=ToolSchema(
                name=descriptor.model_name,
                description=descriptor.description,
                input_schema=descriptor.input_schema,
            ),
            canonical_input_schema=_canonical_input_schema(descriptor),
        )
        for descriptor in permitted
    )
    wire = [
        {
            "name": entry.schema.name,
            "description": entry.schema.description,
            "input_schema": entry.schema.input_schema,
        }
        for entry in entries
    ]
    byte_size = len(canonical_json_text(cast("JsonValue", wire)).encode("utf-8"))
    if byte_size > CATALOG_MAX_BYTES:
        raise ToolCatalogInvalid("catalog is too large")
    return ToolCatalog(
        entries=entries,
        byte_size=byte_size,
        _by_model_name={entry.model_name: entry for entry in entries},
    )
