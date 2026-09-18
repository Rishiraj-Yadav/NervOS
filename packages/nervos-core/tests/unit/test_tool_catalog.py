"""D4 catalog assembly: grant filtering, deterministic ordering, and the frozen bounds.

The catalog is what the model is *offered*. These tests pin the two properties that make it safe to
change: membership is decided by the live permission evaluator rather than by anything the catalog
knows, and the bound is a refusal rather than a truncation.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from nervos_core.application.tool_catalog import (
    CATALOG_MAX_TOOLS,
    ToolCatalogInvalid,
    assemble_tool_catalog,
)
from nervos_core.application.tool_permissions import (
    PermissionDecision,
    PermissionDenialReason,
)
from nervos_core.domain.tools import JsonValue, RiskHints, ToolDescriptor, ToolSourceKind


class _Authorizer:
    """A permission double that allows exactly the ids it was given."""

    def __init__(self, allowed: set[int]) -> None:
        self.allowed = allowed
        self.calls: list[tuple[int, int]] = []

    def check_permission(self, *, run_id: int, tool_definition_id: int) -> PermissionDecision:
        self.calls.append((run_id, tool_definition_id))
        if tool_definition_id in self.allowed:
            return PermissionDecision(allowed=True)
        return PermissionDecision(allowed=False, reason=PermissionDenialReason.NOT_GRANTED)


def _descriptor(
    tool_definition_id: int,
    *,
    model_name: str | None = None,
    description: str = "A tool.",
    input_schema: dict[str, JsonValue] | None = None,
) -> ToolDescriptor:
    return ToolDescriptor(
        tool_definition_id=tool_definition_id,
        upstream_name=f"tool_{tool_definition_id}",
        model_name=model_name or f"nervos__builtin__tool_{tool_definition_id}",
        source_kind=ToolSourceKind.BUILTIN,
        source_id=None,
        display_name=f"Tool {tool_definition_id}",
        description=description,
        input_schema=input_schema or {"type": "object", "properties": {}},
        output_schema=None,
        risk_hints=RiskHints(),
        fingerprint="0" * 64,
    )


class TestFiltering:
    def test_only_permitted_descriptors_are_presented(self) -> None:
        authorizer = _Authorizer({2})
        catalog = assemble_tool_catalog(
            descriptors=[_descriptor(1), _descriptor(2), _descriptor(3)],
            run_id=7,
            authorize=authorizer,
        )

        assert [entry.descriptor.tool_definition_id for entry in catalog.entries] == [2]

    def test_every_candidate_is_checked_against_the_live_evaluator(self) -> None:
        authorizer = _Authorizer(set())
        assemble_tool_catalog(
            descriptors=[_descriptor(1), _descriptor(2)], run_id=7, authorize=authorizer
        )

        assert authorizer.calls == [(7, 1), (7, 2)]

    def test_membership_itself_grants_nothing_the_catalog_only_lists(self) -> None:
        catalog = assemble_tool_catalog(
            descriptors=[_descriptor(5)], run_id=1, authorize=_Authorizer({5})
        )

        # The catalog exposes lookup and schemas; it has no operation that could authorize a call.
        assert not hasattr(catalog, "authorize")
        assert not hasattr(catalog, "check_permission")

    def test_an_ungranted_descriptor_never_becomes_offerable(self) -> None:
        catalog = assemble_tool_catalog(
            descriptors=[_descriptor(1)], run_id=1, authorize=_Authorizer(set())
        )

        assert len(catalog) == 0
        assert catalog.lookup("nervos__builtin__tool_1") is None
        assert catalog.schemas() == ()


class TestOrderingAndLookup:
    def test_entries_are_ordered_by_model_name_regardless_of_input_order(self) -> None:
        descriptors = [
            _descriptor(1, model_name="nervos__builtin__zebra"),
            _descriptor(2, model_name="nervos__builtin__alpha"),
            _descriptor(3, model_name="nervos__builtin__middle"),
        ]
        authorizer = _Authorizer({1, 2, 3})

        forward = assemble_tool_catalog(descriptors=descriptors, run_id=1, authorize=authorizer)
        reversed_ = assemble_tool_catalog(
            descriptors=list(reversed(descriptors)), run_id=1, authorize=authorizer
        )

        names = [entry.model_name for entry in forward.entries]
        assert names == sorted(names)
        # Ordering is fixed before measurement, so the same set always costs the same bytes.
        assert forward.byte_size == reversed_.byte_size

    def test_lookup_returns_the_offered_entry_by_model_name(self) -> None:
        catalog = assemble_tool_catalog(
            descriptors=[_descriptor(4, model_name="nervos__builtin__four")],
            run_id=1,
            authorize=_Authorizer({4}),
        )

        entry = catalog.lookup("nervos__builtin__four")
        assert entry is not None
        assert entry.descriptor.tool_definition_id == 4
        assert entry.schema.name == "nervos__builtin__four"

    def test_an_unknown_name_is_absence_rather_than_an_error(self) -> None:
        catalog = assemble_tool_catalog(
            descriptors=[_descriptor(4)], run_id=1, authorize=_Authorizer({4})
        )

        assert catalog.lookup("nervos__builtin__absent") is None

    def test_the_schema_projection_carries_the_model_name_and_input_schema(self) -> None:
        schema: dict[str, JsonValue] = {
            "type": "object",
            "properties": {"a": {"type": "string"}},
        }
        catalog = assemble_tool_catalog(
            descriptors=[_descriptor(4, model_name="nervos__builtin__one", input_schema=schema)],
            run_id=1,
            authorize=_Authorizer({4}),
        )

        assert catalog.schemas()[0].input_schema == schema


class TestFrozenBounds:
    def test_the_tool_count_bound_is_accepted_at_its_limit(self) -> None:
        descriptors = [_descriptor(index, model_name=f"n{index:04d}") for index in range(1, 33)]
        catalog = assemble_tool_catalog(
            descriptors=descriptors, run_id=1, authorize=_Authorizer(set(range(1, 33)))
        )

        assert len(catalog) == CATALOG_MAX_TOOLS

    def test_one_tool_over_the_count_bound_fails_rather_than_truncating(self) -> None:
        descriptors = [_descriptor(index, model_name=f"n{index:04d}") for index in range(1, 34)]
        with pytest.raises(ToolCatalogInvalid):
            assemble_tool_catalog(
                descriptors=descriptors, run_id=1, authorize=_Authorizer(set(range(1, 34)))
            )

    def test_the_byte_bound_is_measured_over_the_whole_ordered_array(self) -> None:
        catalog = assemble_tool_catalog(
            descriptors=[_descriptor(1, model_name="nervos__builtin__one")],
            run_id=1,
            authorize=_Authorizer({1}),
        )
        assert catalog.byte_size > 0

    def test_a_catalog_over_the_byte_bound_fails_rather_than_dropping_entries(self) -> None:
        # Each tool is individually legal -- a description is bounded at the catalog budget -- so
        # the bound can only be exceeded by the array as a whole, which is what it measures.
        descriptors = [
            replace(_descriptor(index, model_name=f"n{index:04d}"), description="d" * 30_000)
            for index in range(1, 4)
        ]
        with pytest.raises(ToolCatalogInvalid):
            assemble_tool_catalog(
                descriptors=descriptors, run_id=1, authorize=_Authorizer({1, 2, 3})
            )

    def test_an_empty_catalog_is_valid_and_measures_nothing(self) -> None:
        catalog = assemble_tool_catalog(descriptors=[], run_id=1, authorize=_Authorizer(set()))

        assert len(catalog) == 0
        assert catalog.byte_size == len("[]")
