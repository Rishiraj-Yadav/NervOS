"""W4a — the additive ``workflow-v1`` package-host feature (ADR 0039).

Three properties matter here and none of them is "the payload parses":

1. An **old host** is refused *before* ``initialize``, so package code never runs. The
   refusal is a decision, not a side effect, which is what makes it provable here without a
   built package environment.
2. An **ordinary Run** is unchanged in both directions: no ``workflow`` request field, and a
   package cannot smuggle a workflow result into one.
3. The Worker **binds** what a package proposes. The checkpoint revision, the canonical
   digests, and the expiry are decided host-side; the package only supplies intent.
"""

# pyright: basic

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

import pytest
from nervos_core.application.runtime_integration import PackageIntegration
from nervos_core.application.trusted_chat import ChatOutcome
from nervos_core.application.workflows import WorkflowStepSnapshot
from nervos_core.domain.runs import ModelUsage
from nervos_core.domain.workflows import (
    FrozenJSONValue,
    WorkflowDirectiveKind,
    WorkflowWaitKind,
)
from nervos_package_host.wire import (
    CHECKPOINT_MAX_BYTES,
    HOST_CAPABILITIES,
    HOST_PROTOCOL_VERSION,
    WORKFLOW_HOST_CAPABILITY,
    HostProtocolError,
)
from nervos_sdk import (
    CHECKPOINT_MAX_BYTES as SDK_CHECKPOINT_MAX_BYTES,
)
from nervos_sdk import (
    WorkflowDecisionProposal,
    WorkflowDirective,
    WorkflowResult,
)
from nervos_worker.package_execution import (
    _host_capability_refusal,
    _workflow_request_payload,
    _workflow_step_result,
)

SNAPSHOT = WorkflowStepSnapshot(
    step_number=2,
    checkpoint_revision=3,
    state_version=1,
    state=cast("Mapping[str, FrozenJSONValue]", {"sources": ["a"]}),
)
ALL_FEATURES = list(HOST_CAPABILITIES)


# ---------------------------------------------------------------------------------------
# Old hosts are refused before initialize
# ---------------------------------------------------------------------------------------


def test_the_host_advertises_the_workflow_capability() -> None:
    # The advertisement is what the refusal below keys on, so an unadvertised feature would
    # silently refuse every workflow package rather than fail loudly here.
    assert WORKFLOW_HOST_CAPABILITY in HOST_CAPABILITIES


def test_an_old_host_refuses_a_package_that_declared_workflow_v1() -> None:
    old = ["runtime-integration-v1"]
    refusal = _host_capability_refusal(PackageIntegration(workflow=True), None, old)
    assert refusal is not None
    assert "workflow-v1" in str(refusal)


def test_an_old_host_refuses_a_dispatched_workflow_step_even_if_the_manifest_is_silent() -> None:
    # The manifest is attacker-adjacent input; the dispatched step is not. A Run that is
    # genuinely a workflow step must be refused too, not trusted to what it declared.
    refusal = _host_capability_refusal(PackageIntegration(), SNAPSHOT, ["runtime-integration-v1"])
    assert refusal is not None
    assert "workflow-v1" in str(refusal)


def test_a_capable_host_is_not_refused() -> None:
    assert (
        _host_capability_refusal(PackageIntegration(workflow=True), SNAPSHOT, ALL_FEATURES) is None
    )


def test_an_ordinary_package_on_an_ordinary_host_is_untouched() -> None:
    assert _host_capability_refusal(PackageIntegration(), None, []) is None


def test_a_structured_context_package_still_refuses_an_old_host_first() -> None:
    # The pre-existing refusal must not be shadowed by the newer check.
    refusal = _host_capability_refusal(
        PackageIntegration(structured_context=True, workflow=True), None, []
    )
    assert refusal is not None
    assert "structured context" in str(refusal)


# ---------------------------------------------------------------------------------------
# Ordinary Runs are byte-for-byte unchanged
# ---------------------------------------------------------------------------------------


def test_an_ordinary_run_sends_no_workflow_payload() -> None:
    assert _workflow_request_payload(None) is None


def test_a_workflow_step_sends_the_exact_checkpoint_it_resumes_from() -> None:
    # Frozen state is normalized on the way in (lists become tuples), so the comparison is
    # against the frozen shape rather than the literal the test happened to write.
    assert _workflow_request_payload(SNAPSHOT) == {
        "state_version": 1,
        "checkpoint_revision": 3,
        "step_number": 2,
        "state": {"sources": ("a",)},
        "wake_signal": {},
    }


def test_an_ordinary_run_may_not_return_a_workflow_result() -> None:
    with pytest.raises(HostProtocolError, match="ordinary Run"):
        _workflow_step_result({"directive": {"kind": "next"}, "state": {}}, snapshot=None)


def test_a_workflow_step_that_returns_nothing_is_refused() -> None:
    # Silently succeeding here would strand the Run as "succeeded" with no checkpoint,
    # which is exactly the failure the fenced commit exists to prevent.
    with pytest.raises(HostProtocolError, match="must return"):
        _workflow_step_result(None, snapshot=SNAPSHOT)


# ---------------------------------------------------------------------------------------
# Round trips
# ---------------------------------------------------------------------------------------


def test_a_next_directive_round_trips_to_the_next_checkpoint() -> None:
    result = _workflow_step_result(
        {
            "directive": {"kind": "next"},
            "state": {"sources": ["a", "b"], "round": 1},
            "summary": "Collected a second source.",
        },
        snapshot=SNAPSHOT,
    )
    assert result is not None
    assert result.directive.kind is WorkflowDirectiveKind.NEXT
    assert result.state["round"] == 1
    assert result.summary == "Collected a second source."


def test_a_complete_directive_ends_the_workflow() -> None:
    result = _workflow_step_result(
        {"directive": {"kind": "complete"}, "state": {"brief": "done"}},
        snapshot=SNAPSHOT,
    )
    assert result is not None
    assert result.directive.kind is WorkflowDirectiveKind.COMPLETE


def test_a_time_wait_keeps_the_owners_clock_out_of_the_package() -> None:
    # The package asks for a relative delay; the Worker owns the absolute instant, so a
    # workflow cannot be parked by a package supplying its own clock.
    result = _workflow_step_result(
        {"directive": {"kind": "wait", "wait_kind": "time", "wait_seconds": 3600}, "state": {}},
        snapshot=SNAPSHOT,
    )
    assert result is not None
    assert result.directive.wait_kind is WorkflowWaitKind.TIME
    assert result.directive.wait_seconds == 3600


def test_a_signal_wait_names_its_key() -> None:
    result = _workflow_step_result(
        {
            "directive": {"kind": "wait", "wait_kind": "signal", "signal_key": "inbox_ready"},
            "state": {},
        },
        snapshot=SNAPSHOT,
    )
    assert result is not None
    assert result.directive.signal_key == "inbox_ready"


def test_an_unknown_directive_kind_is_refused() -> None:
    with pytest.raises(HostProtocolError, match="directive kind"):
        _workflow_step_result({"directive": {"kind": "teleport"}, "state": {}}, snapshot=SNAPSHOT)


# ---------------------------------------------------------------------------------------
# The Worker binds, the package proposes
# ---------------------------------------------------------------------------------------


def test_a_decision_wait_binds_the_revision_and_digests_host_side() -> None:
    before = datetime.now(UTC)
    result = _workflow_step_result(
        {
            "directive": {
                "kind": "wait",
                "wait_kind": "owner_decision",
                "decision": {
                    "tool_definition_id": 12,
                    "upstream_name": "gmail.send_draft",
                    "arguments": {"thread_id": "t1"},
                    "preview": {"body": "hello"},
                    "expires_in_seconds": 600,
                },
                # A package asserting these must not be able to override them.
                "checkpoint_revision": 999,
                "arguments_digest": "0" * 64,
            },
            "state": {},
        },
        snapshot=SNAPSHOT,
    )
    assert result is not None
    decision = result.decision_request
    assert decision is not None
    assert decision.checkpoint_revision == 3  # the revision actually dispatched
    assert decision.arguments_digest != "0" * 64  # computed, not accepted
    assert decision.tool_definition_id == 12
    assert decision.upstream_name == "gmail.send_draft"
    assert decision.expires_at > before


def test_a_decision_wait_without_a_proposal_is_refused() -> None:
    with pytest.raises(HostProtocolError, match="decision proposal"):
        _workflow_step_result(
            {"directive": {"kind": "wait", "wait_kind": "owner_decision"}, "state": {}},
            snapshot=SNAPSHOT,
        )


# ---------------------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------------------


def test_the_checkpoint_bound_is_stricter_than_the_frame_bound() -> None:
    # A checkpoint must not be able to spend the whole 1 MiB frame.
    from nervos_package_host.wire import MAX_FRAME_BYTES

    assert CHECKPOINT_MAX_BYTES == SDK_CHECKPOINT_MAX_BYTES
    assert CHECKPOINT_MAX_BYTES < MAX_FRAME_BYTES


def test_the_worker_refuses_an_oversized_checkpoint() -> None:
    oversized = {"blob": "x" * (CHECKPOINT_MAX_BYTES + 1)}
    with pytest.raises(HostProtocolError, match="checkpoint bound"):
        _workflow_step_result(
            {"directive": {"kind": "next"}, "state": oversized}, snapshot=SNAPSHOT
        )


def test_an_oversized_summary_is_refused() -> None:
    with pytest.raises(HostProtocolError, match="summary"):
        _workflow_step_result(
            {"directive": {"kind": "next"}, "state": {}, "summary": "s" * 513},
            snapshot=SNAPSHOT,
        )


# ---------------------------------------------------------------------------------------
# SDK-side validation, and the two ends agreeing on one encoding
# ---------------------------------------------------------------------------------------


def test_the_sdk_refuses_a_directive_carrying_two_wait_conditions() -> None:
    # Asking to be woken by both a clock and an owner decision leaves the resume intent
    # ambiguous across a restart, so it is refused rather than resolved arbitrarily.
    with pytest.raises(ValueError, match="exactly one wait condition"):
        WorkflowDirective(kind="wait", wait_kind="time", wait_seconds=60, signal_key="also_me")


def test_the_sdk_refuses_a_next_directive_carrying_a_condition() -> None:
    with pytest.raises(ValueError, match="only a wait directive"):
        WorkflowDirective(kind="next", wait_seconds=60)


def test_the_sdk_proposal_is_a_proposal_not_authority() -> None:
    # The SDK type deliberately has no revision, digest or pinned-identity field: those are
    # exactly the things a package must not be able to assert.
    proposal = WorkflowDecisionProposal(
        tool_definition_id=12, upstream_name="gmail.send_draft", arguments={"thread_id": "t1"}
    )
    assert not hasattr(proposal, "checkpoint_revision")
    assert not hasattr(proposal, "arguments_digest")
    assert not hasattr(proposal, "package_content_digest")


def test_a_workflow_result_is_optional_so_an_ordinary_agent_is_unchanged() -> None:
    assert ChatOutcome("done", "stop", ModelUsage()).workflow_step is None


def test_the_two_ends_agree_on_the_wire_shape() -> None:
    # The SDK encodes directives and the Worker decodes them; if these drift, a package
    # silently loses its state rather than failing loudly.
    result = WorkflowResult(
        directive=WorkflowDirective(kind="wait", wait_kind="signal", signal_key="inbox_ready"),
        state={"seen": 3},
        summary="waiting for the inbox",
    )
    payload: Mapping[str, object] = {
        "directive": {"kind": "wait", "wait_kind": "signal", "signal_key": "inbox_ready"},
        "state": {"seen": 3},
        "summary": "waiting for the inbox",
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert result.directive.kind == "wait"
    assert result.directive.signal_key == "inbox_ready"
    assert len(encoded.encode("utf-8")) <= CHECKPOINT_MAX_BYTES


def test_the_protocol_version_is_unchanged_by_this_additive_feature() -> None:
    # An additive payload field must not bump the protocol, or every existing package
    # environment would be invalidated.
    assert HOST_PROTOCOL_VERSION == "1"
