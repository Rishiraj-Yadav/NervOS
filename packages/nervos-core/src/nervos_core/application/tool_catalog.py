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
from typing import cast

from nervos_core.application.model_completion import ToolSchema
from nervos_core.application.tool_permissions import ToolPermissionEvaluator
from nervos_core.application.tool_schema import (
    CanonicalSchema,
    SchemaRejection,
    validate_canonical_schema,
)
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
