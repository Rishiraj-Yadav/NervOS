"""Owner-facing workflow projections for the API and dashboard (ADR 0039, W5b).

A thin layer over :class:`~nervos_core.application.workflows.WorkflowService` that decides
one thing and one thing only: **what an owner is allowed to see.** It decides no transitions,
schedules nothing, and grants nothing.

The projection rules are the substance here, not the field lists:

* The **list** projection carries no checkpoint content at all. A workflow list is a
  dashboard summary, and application state is not a summary.
* The **detail** projection carries a bounded checkpoint *shape* -- revision, byte count,
  top-level key names -- rather than the values. An owner can confirm that a step stored
  what they expected without the dashboard becoming a renderer for arbitrary application
  state.
* Recovery guidance is derived from the workflow's own recorded reason, never invented, so
  the UI cannot advise an action the runtime did not actually take.
* A **decision** carries its ``expected_revision`` requirement through to the caller rather
  than defaulting it, because deciding against a revision the owner never saw is exactly the
  stale-authorization case the revision exists to prevent.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, cast

from nervos_core.application.workflows import (
    WorkflowDecisionView,
    WorkflowService,
    WorkflowSignalView,
    WorkflowStepView,
)
from nervos_core.domain.tools import JsonValue
from nervos_core.domain.workflows import (
    REVIEW_BUDGET_EXHAUSTED,
    REVIEW_CONFIGURATION_CHANGED,
    REVIEW_DEADLINE_EXCEEDED,
    REVIEW_RUN_TERMINAL,
    REVIEW_STEP_LIMIT,
    TERMINAL_WORKFLOW_STATUSES,
    WorkflowBudget,
    WorkflowExecution,
    WorkflowNotFound,
    WorkflowReservations,
    WorkflowStatus,
)

#: The recovery advice shown for each reason the runtime itself recorded. A workflow only
#: reaches ``needs_review`` for one of these, so an unmapped reason is a defect rather than
#: something to paper over with generic text.
_RECOVERY: Mapping[str, tuple[str, str]] = {
    REVIEW_RUN_TERMINAL: (
        "A Run ended without committed workflow progress.",
        "Inspect its timeline and tool receipts before starting new work. "
        "Do not replay uncertain effects.",
    ),
    REVIEW_DEADLINE_EXCEEDED: (
        "The workflow passed its deadline before it finished.",
        "Start a new workflow with a longer budget.",
    ),
    REVIEW_STEP_LIMIT: (
        "The workflow used every step it was allowed.",
        "Start a new workflow with a larger step budget.",
    ),
    REVIEW_BUDGET_EXHAUSTED: (
        "The workflow exhausted its reserved model, tool, or output budget.",
        "Start a new workflow with a larger reservation.",
    ),
    REVIEW_CONFIGURATION_CHANGED: (
        "The agent's configuration or package changed after this workflow started.",
        "Start a new workflow. This one will not resume against the new identity.",
    ),
}


class WorkflowViewService:
    """Owner-safe JSON projections over the durable workflow control plane."""

    def __init__(
        self,
        service: WorkflowService,
        *,
        create: Callable[..., dict[str, Any]],
    ) -> None:
        self._service = service
        self._create = create

    # -- list ----------------------------------------------------------------------------

    def list_workflows(
        self, owner_user_id: int, *, limit: int, before_id: int | None
    ) -> dict[str, Any]:
        rows = self._service.list(owner_user_id, limit=limit, before_id=before_id)
        return {
            "workflows": [self.summary(row) for row in rows],
            # Keyset pagination: the next page starts before this page's last id.
            "next_before_id": rows[-1].id if len(rows) == limit else None,
        }

    def summary(self, workflow: WorkflowExecution) -> dict[str, Any]:
        return {
            "id": workflow.id,
            "workflow_kind": workflow.workflow_kind,
            "status": workflow.status.value,
            "paused": workflow.paused,
            "step_count": workflow.step_count,
            "checkpoint_revision": workflow.checkpoint_revision,
            "wait_kind": workflow.wait_kind.value if workflow.wait_kind else None,
            "signal_key": workflow.signal_key,
            "decision_key": workflow.decision_key,
            "wakeup_at": _iso(workflow.wakeup_at),
            "deadline_at": _iso(workflow.deadline_at),
            "created_at": _iso(workflow.created_at),
            "finished_at": _iso(workflow.finished_at),
            "review_reason": workflow.review_reason,
            "budget": _budget(workflow.budget, workflow.reservations, workflow.step_count),
        }

    def checkpoint(self, owner_user_id: int, workflow_id: int, revision: int) -> dict[str, Any]:
        """Explicit private inspection; lists and normal details never carry state values."""
        detail = self._service.detail(owner_user_id, workflow_id)
        checkpoint = next((c for c in detail.checkpoints if c.revision == revision), None)
        if checkpoint is None:
            raise WorkflowNotFound("checkpoint not found")
        return {"revision": revision, "state": _plain(checkpoint.state)}

    # -- detail --------------------------------------------------------------------------

    def workflow_detail(self, owner_user_id: int, workflow_id: int) -> dict[str, Any]:
        detail = self._service.detail(owner_user_id, workflow_id)
        workflow = detail.workflow
        return {
            "workflow": self.summary(workflow),
            "steps": [_step(step) for step in detail.steps],
            "checkpoints": [_checkpoint_shape(item) for item in detail.checkpoints],
            "decisions": [_decision(item) for item in detail.decisions],
            "signals": [_signal(item) for item in detail.signals],
            "recovery": self.workflow_recovery(owner_user_id, workflow_id),
        }

    # -- controls -----------------------------------------------------------------------

    def set_paused(self, owner_user_id: int, workflow_id: int, paused: bool) -> dict[str, Any]:
        # Pause and resume are separate verbs on the control plane. Routing them through
        # one flag means the UI cannot express "stop the next step" and "abandon this" as
        # the same action, which is what W5b asks for.
        call = self._service.pause if paused else self._service.resume
        return self.summary(call(owner_user_id, workflow_id))

    def cancel_workflow(self, owner_user_id: int, workflow_id: int) -> dict[str, Any]:
        return self.summary(self._service.cancel(owner_user_id, workflow_id))

    def deliver_signal(
        self,
        *,
        owner_user_id: int,
        workflow_id: int,
        signal_key: str,
        payload: Mapping[str, JsonValue],
        expected_revision: int,
    ) -> dict[str, Any]:
        outcome = self._service.signal(
            owner_user_id,
            workflow_id,
            signal_key=signal_key,
            payload=payload,
            expected_revision=expected_revision,
        )
        return {
            "accepted": outcome.accepted,
            "outcome": outcome.outcome,
            "workflow": self.summary(outcome.workflow),
        }

    def workflow_decisions(self, owner_user_id: int, workflow_id: int) -> dict[str, Any]:
        rows = self._service.list_decisions(owner_user_id, workflow_id)
        return {"decisions": [_decision(row) for row in rows]}

    def decide(
        self,
        *,
        owner_user_id: int,
        workflow_id: int,
        decision_id: int,
        approve: bool,
        expected_revision: int,
    ) -> dict[str, Any]:
        # An owner decision about *proposed work*. It is not the H3 live-dispatch approval:
        # approving here lets one step proceed to its own dispatch boundary, where live
        # grants, account state and cancellation are rechecked.
        view = self._service.decide(
            owner_user_id,
            workflow_id,
            decision_id,
            approve=approve,
            expected_revision=expected_revision,
        )
        return _decision(view)

    def create_workflow(
        self,
        *,
        owner_user_id: int,
        agent_instance_id: int,
        submission_key: str,
        input_text: str,
        workflow_kind: str,
    ) -> dict[str, Any]:
        return self._create(
            owner_user_id=owner_user_id,
            agent_instance_id=agent_instance_id,
            submission_key=submission_key,
            input_text=input_text,
            workflow_kind=workflow_kind,
        )

    # -- recovery -----------------------------------------------------------------------

    def workflow_recovery(self, owner_user_id: int, workflow_id: int) -> dict[str, Any]:
        """Safe guidance, derived only from what the runtime actually recorded."""
        workflow = self._service.get(owner_user_id, workflow_id)
        needs_review = workflow.status is WorkflowStatus.NEEDS_REVIEW
        terminal = workflow.status in TERMINAL_WORKFLOW_STATUSES
        if not needs_review and not terminal:
            return {
                "needs_attention": False,
                "summary": "This workflow is advancing normally.",
                "actions": ["resume", "cancel"] if workflow.paused else ["pause", "cancel"],
            }
        reason = workflow.review_reason
        known = _RECOVERY.get(reason or "")
        if needs_review and known is None:
            # The runtime recorded a reason this projection does not know. Saying so is
            # better than inventing advice the runtime never asked for.
            return {
                "needs_attention": True,
                "summary": "This workflow needs attention.",
                "detail": reason,
                "actions": ["cancel"],
            }
        summary, guidance = known or (
            "This workflow finished.",
            "Its result is retained; start a new workflow to continue.",
        )
        return {
            "needs_attention": needs_review,
            "summary": summary,
            "detail": reason,
            "guidance": guidance,
            "actions": [] if terminal else ["cancel"],
        }


# ---------------------------------------------------------------------------------------
# Projection helpers
# ---------------------------------------------------------------------------------------


def _iso(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _budget(
    budget: WorkflowBudget, reservations: WorkflowReservations, step_count: int
) -> dict[str, Any]:
    models, tools, tokens = reservations.remaining(budget)
    return {
        "steps_used": step_count,
        "steps_allowed": budget.max_steps,
        "model_calls_reserved": reservations.model_calls,
        "model_calls_remaining": max(models, 0),
        "tool_calls_reserved": reservations.tool_calls,
        "tool_calls_remaining": max(tools, 0),
        "output_tokens_reserved": reservations.output_tokens,
        "output_tokens_remaining": max(tokens, 0),
    }


def _step(view: WorkflowStepView) -> dict[str, Any]:
    return {
        "step_number": view.step.step_number,
        "run_id": view.run_id,
        "status": view.step.status.value,
        "run_status": view.run_status,
        "job_phase": view.job_phase,
        "summary": view.step.summary,
        "expected_checkpoint_revision": view.step.expected_checkpoint_revision,
        "finished_at": _iso(view.step.finished_at),
    }


def _checkpoint_shape(checkpoint: object) -> dict[str, Any]:
    """Revision, size and key names -- never the values.

    An owner can confirm that a step stored what they expected without the dashboard
    becoming a renderer for arbitrary application state.
    """
    state = getattr(checkpoint, "state", {})
    encoded = json.dumps(_plain(state), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {
        "revision": getattr(checkpoint, "revision", 0),
        "step_number": getattr(checkpoint, "step_number", 0),
        "byte_size": len(encoded.encode("utf-8")),
        "keys": sorted(str(key) for key in dict(state))[:32],
        "created_at": _iso(getattr(checkpoint, "created_at", None)),
    }


def _decision(view: WorkflowDecisionView) -> dict[str, Any]:
    return {
        "id": view.id,
        "checkpoint_revision": view.checkpoint_revision,
        "tool_definition_id": view.tool_definition_id,
        "upstream_name": view.upstream_name,
        "arguments_digest": view.arguments_digest,
        # The preview is the exact action being authorized, so it is shown. It was bounded
        # by the decision's own limits at the moment it was recorded.
        "preview": _plain(view.preview),
        "state": view.state.value,
        "requested_at": _iso(view.requested_at),
        "expires_at": _iso(view.expires_at),
        "decided_at": _iso(view.decided_at),
        "consumed_at": _iso(view.consumed_at),
    }


def _signal(view: WorkflowSignalView) -> dict[str, Any]:
    return {
        "id": view.id,
        "signal_key": view.signal_key,
        "expected_revision": view.expected_revision,
        "received_at": _iso(view.received_at),
        "accepted_at": _iso(view.accepted_at),
        "outcome": view.outcome,
    }


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in cast(Mapping[str, Any], value).items()}
    if isinstance(value, tuple | list):
        return [_plain(item) for item in cast(list[Any], value)]
    return value
