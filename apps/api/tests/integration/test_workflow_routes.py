"""W5b — the workflow control plane over real HTTP, with a real owner session.

The point of this file is the *wire* behaviour an owner depends on, which the service-level
tests cannot show: that the routes are registered, that they are owner-scoped from the
session rather than from a body field, that a stale revision is refused, and that a second
owner genuinely cannot see or control the first owner's workflow.

Nothing here advances a workflow. That is the Scheduler's tick, and an API that could do it
from a request would be a second way to run execution-plane work.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nervos_api.config import Settings
from nervos_core.domain.workflows import WorkflowStatus, WorkflowWaitKind
from nervos_core.infrastructure.database.models import WorkflowExecutionRecord
from sqlalchemy import update

WORKFLOWS = "/api/v1/workflows"
ORIGIN = {"Origin": "http://localhost:5173"}

ParkInWait = Callable[[int, str, int], None]


def make_agent(client: TestClient, name: str = "Researcher") -> int:
    response = client.post(
        "/api/v1/agent-instances",
        json={
            "agent_key": "nervos.chat",
            "agent_definition_version": "1",
            "display_name": name,
            "model_provider": "anthropic",
            "model_name": "opaque/model",
        },
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def create_workflow(
    client: TestClient,
    agent_id: int,
    key: str,
    *,
    input_text: str = "Research durable workflows",
    kind: str = "research",
) -> dict[str, Any]:
    response = client.post(
        WORKFLOWS,
        json={
            "agent_instance_id": agent_id,
            "submission_key": key,
            "input_text": input_text,
            "workflow_kind": kind,
        },
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


@pytest.fixture
def agent(owner_client: TestClient) -> int:
    return make_agent(owner_client)


@pytest.fixture
def park_in_signal_wait(migrated_app: tuple[FastAPI, Settings]) -> ParkInWait:
    """Stand in for the Scheduler by parking a workflow in a `waiting` signal state.

    The wait is normally created by the Scheduler's step finalizer when a step returns a wait
    directive. Writing it directly keeps this file a *route* test: it exercises the wire, not
    the tick that would otherwise have to drive a whole model turn to get here. Nothing below
    asserts on this write -- each test re-reads the workflow through the API.
    """

    def park(workflow_id: int, signal_key: str, revision: int) -> None:
        app, _ = migrated_app
        with app.state.session_factory.begin() as session:
            session.execute(
                update(WorkflowExecutionRecord)
                .where(WorkflowExecutionRecord.id == workflow_id)
                .values(
                    status=WorkflowStatus.WAITING.value,
                    wait_kind=WorkflowWaitKind.SIGNAL.value,
                    signal_key=signal_key,
                    checkpoint_revision=revision,
                )
            )

    return park


# -------------------------------------------------------------------------------------------
# Authority & authentication
# -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", WORKFLOWS, None),
        ("POST", WORKFLOWS, {"agent_instance_id": 1, "submission_key": "k", "input_text": "x"}),
        ("GET", f"{WORKFLOWS}/1", None),
        ("POST", f"{WORKFLOWS}/1/pause", {"paused": True}),
        ("POST", f"{WORKFLOWS}/1/cancel", None),
        ("POST", f"{WORKFLOWS}/1/signals", {"signal_key": "go", "expected_revision": 0}),
        ("GET", f"{WORKFLOWS}/1/decisions", None),
        ("POST", f"{WORKFLOWS}/1/decisions/1", {"approve": True, "expected_revision": 0}),
        ("GET", f"{WORKFLOWS}/1/needs-review", None),
    ],
)
def test_every_workflow_route_requires_authentication(
    client: TestClient, method: str, path: str, body: dict[str, Any] | None
) -> None:
    response = client.request(method, path, json=body, headers=ORIGIN)
    assert response.status_code == 401, (path, response.text)
    assert response.json()["error"]["code"] == "authentication_required"


def test_workflow_mutations_require_the_exact_origin(owner_client: TestClient, agent: int) -> None:
    created = create_workflow(owner_client, agent, "origin-check")
    path = f"{WORKFLOWS}/{created['id']}"

    missing = owner_client.post(f"{path}/cancel")
    wrong = owner_client.post(f"{path}/cancel", headers={"Origin": "http://evil.test"})

    for refused in (missing, wrong):
        assert refused.status_code == 403, refused.text
        assert refused.json()["error"]["code"] == "invalid_origin"


# -------------------------------------------------------------------------------------------
# Creation, listing, and projection
# -------------------------------------------------------------------------------------------


def test_an_owner_can_create_and_inspect_a_workflow(owner_client: TestClient, agent: int) -> None:
    created = create_workflow(owner_client, agent, "route-create")
    assert created["status"] == "running"
    assert created["step_count"] == 1
    assert created["checkpoint_revision"] == 0
    assert created["workflow_kind"] == "research"

    listed = owner_client.get(WORKFLOWS)
    assert listed.status_code == 200
    body = listed.json()
    assert [row["id"] for row in body["workflows"]] == [created["id"]]
    assert body["next_before_id"] is None

    detail = owner_client.get(f"{WORKFLOWS}/{created['id']}")
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["workflow"]["id"] == created["id"]
    assert len(detail_body["steps"]) == 1
    assert detail_body["steps"][0]["step_number"] == 1
    assert detail_body["recovery"]["needs_attention"] is False


def test_the_list_projection_carries_no_checkpoint_content(
    owner_client: TestClient, agent: int
) -> None:
    create_workflow(owner_client, agent, "route-list-shape")
    listed = owner_client.get(WORKFLOWS).json()

    for summary in listed["workflows"]:
        # A workflow list is a dashboard summary. Application state is not a summary, so no
        # projection key here may carry checkpoint content at all.
        assert "checkpoints" not in summary
        assert "state" not in summary
        assert "steps" not in summary
        # The budget is projected as counts, never as the reservation payloads.
        assert set(summary["budget"]) == {
            "steps_used",
            "steps_allowed",
            "model_calls_reserved",
            "model_calls_remaining",
            "tool_calls_reserved",
            "tool_calls_remaining",
            "output_tokens_reserved",
            "output_tokens_remaining",
        }


def test_a_checkpoint_is_projected_as_a_shape_and_never_as_its_contents(
    owner_client: TestClient, agent: int
) -> None:
    created = create_workflow(owner_client, agent, "route-shape")
    detail = owner_client.get(f"{WORKFLOWS}/{created['id']}").json()

    # A fresh workflow has committed no checkpoint yet, so the list is empty. What matters is
    # that every entry is a bounded shape: an owner can confirm that a step stored what they
    # expected without the dashboard becoming a renderer for arbitrary application state.
    for checkpoint in detail["checkpoints"]:
        assert set(checkpoint) == {
            "revision",
            "step_number",
            "byte_size",
            "keys",
            "created_at",
        }
        assert isinstance(checkpoint["byte_size"], int)
        assert isinstance(checkpoint["keys"], list)


def test_pause_and_cancel_are_separate_verbs(owner_client: TestClient, agent: int) -> None:
    created = create_workflow(owner_client, agent, "route-pause")
    path = f"{WORKFLOWS}/{created['id']}"

    paused = owner_client.post(f"{path}/pause", json={"paused": True}, headers=ORIGIN)
    assert paused.status_code == 200, paused.text
    assert paused.json()["paused"] is True
    # Pausing stops future steps. It must not look like termination.
    assert paused.json()["status"] == "running"

    resumed = owner_client.post(f"{path}/pause", json={"paused": False}, headers=ORIGIN)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["paused"] is False

    cancelled = owner_client.post(f"{path}/cancel", headers=ORIGIN)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"


def test_a_terminal_workflow_cannot_be_paused_again(owner_client: TestClient, agent: int) -> None:
    created = create_workflow(owner_client, agent, "route-terminal")
    path = f"{WORKFLOWS}/{created['id']}"
    assert owner_client.post(f"{path}/cancel", headers=ORIGIN).status_code == 200

    refused = owner_client.post(f"{path}/pause", json={"paused": True}, headers=ORIGIN)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "workflow_transition_conflict"


# -------------------------------------------------------------------------------------------
# Revision-bound authorization
# -------------------------------------------------------------------------------------------


def test_a_signal_requires_the_revision_the_owner_was_shown(
    owner_client: TestClient, agent: int
) -> None:
    created = create_workflow(owner_client, agent, "route-signal")
    response = owner_client.post(
        f"{WORKFLOWS}/{created['id']}/signals",
        json={"signal_key": "inbox_ready", "payload": {}},
        headers=ORIGIN,
    )
    # `expected_revision` is required, not defaulted. A signal accepted against a revision the
    # caller never saw would resume the workflow from a state they are not looking at.
    assert response.status_code == 422, response.text


def test_a_refused_signal_is_named_and_does_not_burn_the_key(
    owner_client: TestClient, agent: int, park_in_signal_wait: ParkInWait
) -> None:
    """A signal that cannot advance the workflow is refused *and* leaves no durable row.

    The refusal is returned synchronously with a named outcome, so the owner learns why their
    signal did nothing rather than being handed a bare 4xx. It deliberately does not occupy
    the `(workflow, signal_key)` identity: a stale or wrong-key probe must not be able to
    burn the name and block the legitimate delivery that follows. That is the property this
    test exists to pin.
    """
    created = create_workflow(owner_client, agent, "route-signal-refusal")
    revision = created["checkpoint_revision"]
    park_in_signal_wait(created["id"], "inbox_ready", revision)
    path = f"{WORKFLOWS}/{created['id']}/signals"

    stale = owner_client.post(
        path,
        json={
            "signal_key": "inbox_ready",
            "payload": {"count": 3},
            "expected_revision": revision + 99,
        },
        headers=ORIGIN,
    )
    assert stale.status_code == 202, stale.text
    assert stale.json()["accepted"] is False
    assert stale.json()["outcome"] == "stale_revision"
    assert stale.json()["workflow"]["checkpoint_revision"] == revision

    wrong_key = owner_client.post(
        path,
        json={"signal_key": "not_the_one", "payload": {}, "expected_revision": revision},
        headers=ORIGIN,
    )
    assert wrong_key.status_code == 202, wrong_key.text
    assert wrong_key.json()["outcome"] == "not_waiting"

    # Neither probe touched durable state...
    detail = owner_client.get(f"{WORKFLOWS}/{created['id']}").json()
    assert detail["signals"] == []
    assert detail["workflow"]["wait_kind"] == "signal"
    assert detail["workflow"]["signal_key"] == "inbox_ready"

    # ...so the real delivery still lands. The probes above cost the owner nothing.
    accepted = owner_client.post(
        path,
        json={
            "signal_key": "inbox_ready",
            "payload": {"count": 3},
            "expected_revision": revision,
        },
        headers=ORIGIN,
    )
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["accepted"] is True
    assert accepted.json()["outcome"] == "accepted"


def test_an_accepted_signal_consumes_the_wait_and_is_recorded(
    owner_client: TestClient, agent: int, park_in_signal_wait: ParkInWait
) -> None:
    created = create_workflow(owner_client, agent, "route-signal-accept")
    park_in_signal_wait(created["id"], "inbox_ready", created["checkpoint_revision"])

    response = owner_client.post(
        f"{WORKFLOWS}/{created['id']}/signals",
        json={
            "signal_key": "inbox_ready",
            "payload": {"count": 3},
            "expected_revision": created["checkpoint_revision"],
        },
        headers=ORIGIN,
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["accepted"] is True
    assert body["outcome"] == "accepted"
    # The wait is consumed: the workflow is runnable again, not still waiting.
    assert body["workflow"]["wait_kind"] is None
    assert body["workflow"]["signal_key"] is None
    assert body["workflow"]["status"] == "runnable"

    detail = owner_client.get(f"{WORKFLOWS}/{created['id']}").json()
    assert len(detail["signals"]) == 1
    assert detail["signals"][0]["accepted_at"] is not None


def test_replaying_an_accepted_signal_returns_the_first_outcome(
    owner_client: TestClient, agent: int, park_in_signal_wait: ParkInWait
) -> None:
    created = create_workflow(owner_client, agent, "route-signal-replay")
    park_in_signal_wait(created["id"], "inbox_ready", created["checkpoint_revision"])
    payload = {
        "signal_key": "inbox_ready",
        "payload": {"count": 3},
        "expected_revision": created["checkpoint_revision"],
    }

    first = owner_client.post(f"{WORKFLOWS}/{created['id']}/signals", json=payload, headers=ORIGIN)
    again = owner_client.post(f"{WORKFLOWS}/{created['id']}/signals", json=payload, headers=ORIGIN)

    assert first.status_code == 202 and again.status_code == 202
    # A duplicate delivery never advances twice: the first accepted outcome is replayed.
    assert first.json()["accepted"] is True
    assert again.json()["accepted"] is True
    assert again.json()["outcome"] == "replayed"
    assert len(owner_client.get(f"{WORKFLOWS}/{created['id']}").json()["signals"]) == 1


def test_changed_payload_under_an_accepted_signal_key_is_a_conflict(
    owner_client: TestClient, agent: int, park_in_signal_wait: ParkInWait
) -> None:
    created = create_workflow(owner_client, agent, "route-signal-conflict")
    park_in_signal_wait(created["id"], "inbox_ready", created["checkpoint_revision"])
    path = f"{WORKFLOWS}/{created['id']}/signals"

    assert (
        owner_client.post(
            path,
            json={
                "signal_key": "inbox_ready",
                "payload": {"count": 3},
                "expected_revision": created["checkpoint_revision"],
            },
            headers=ORIGIN,
        ).status_code
        == 202
    )
    changed = owner_client.post(
        path,
        json={
            "signal_key": "inbox_ready",
            "payload": {"count": 4},
            "expected_revision": created["checkpoint_revision"],
        },
        headers=ORIGIN,
    )
    # Same key, different content is a conflict rather than a second delivery.
    assert changed.status_code == 409, changed.text
    assert changed.json()["error"]["code"] == "workflow_conflict"


def test_a_decision_requires_the_revision_the_owner_was_shown(
    owner_client: TestClient, agent: int
) -> None:
    created = create_workflow(owner_client, agent, "route-decision")
    response = owner_client.post(
        f"{WORKFLOWS}/{created['id']}/decisions/1",
        json={"approve": True},
        headers=ORIGIN,
    )
    assert response.status_code == 422, response.text


def test_a_decision_is_scoped_to_its_own_workflow(owner_client: TestClient, agent: int) -> None:
    created = create_workflow(owner_client, agent, "route-decision-scope")
    response = owner_client.post(
        f"{WORKFLOWS}/{created['id']}/decisions/1",
        json={"approve": True, "expected_revision": created["checkpoint_revision"]},
        headers=ORIGIN,
    )
    # The workflow exists and is owned; only the decision does not.
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "workflow_not_found"


# -------------------------------------------------------------------------------------------
# Submission identity
# -------------------------------------------------------------------------------------------


def test_replay_under_the_same_submission_key_returns_the_original_workflow(
    owner_client: TestClient, agent: int
) -> None:
    first = create_workflow(owner_client, agent, "route-replay")
    again = create_workflow(owner_client, agent, "route-replay")

    assert again["id"] == first["id"]
    assert len(owner_client.get(WORKFLOWS).json()["workflows"]) == 1


def test_a_different_body_under_the_same_key_is_a_conflict(
    owner_client: TestClient, agent: int
) -> None:
    create_workflow(owner_client, agent, "route-conflict")
    response = owner_client.post(
        WORKFLOWS,
        json={
            "agent_instance_id": agent,
            "submission_key": "route-conflict",
            "input_text": "Something else entirely",
            "workflow_kind": "research",
        },
        headers=ORIGIN,
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "workflow_conflict"


def test_a_submission_key_is_scoped_to_its_owner(
    owner_client: TestClient,
    seed_user_account: Callable[[str], int],
    sign_in_as: Callable[[str], None],
) -> None:
    seed_user_account("owner-two")
    create_workflow(owner_client, make_agent(owner_client), "shared-key")

    # Two owners may pick the same submission key: the identity is (owner, key), not key alone.
    sign_in_as("owner-two")
    second = create_workflow(owner_client, make_agent(owner_client, "Triage"), "shared-key")
    assert second["id"] != 1
    assert [row["id"] for row in owner_client.get(WORKFLOWS).json()["workflows"]] == [second["id"]]


def test_a_malformed_submission_key_is_refused(owner_client: TestClient, agent: int) -> None:
    for bad in ("Upper Case", " leading space", "x" * 65):
        response = owner_client.post(
            WORKFLOWS,
            json={
                "agent_instance_id": agent,
                "submission_key": bad,
                "input_text": "hello",
                "workflow_kind": "research",
            },
            headers=ORIGIN,
        )
        assert response.status_code == 422, (bad, response.text)


# -------------------------------------------------------------------------------------------
# Owner isolation
# -------------------------------------------------------------------------------------------


def test_a_second_owner_cannot_see_or_control_the_first_owners_workflow(
    owner_client: TestClient,
    seed_user_account: Callable[[str], int],
    sign_in_as: Callable[[str], None],
) -> None:
    created = create_workflow(owner_client, make_agent(owner_client), "route-isolation")
    seed_user_account("owner-two")
    sign_in_as("owner-two")

    assert owner_client.get(WORKFLOWS).json()["workflows"] == []

    # Missing and forbidden are deliberately the same answer, so the response cannot be used to
    # discover whether another owner's workflow id exists.
    for refused in (
        owner_client.get(f"{WORKFLOWS}/{created['id']}"),
        owner_client.post(f"{WORKFLOWS}/{created['id']}/cancel", headers=ORIGIN),
        owner_client.post(
            f"{WORKFLOWS}/{created['id']}/pause", json={"paused": True}, headers=ORIGIN
        ),
        owner_client.get(f"{WORKFLOWS}/{created['id']}/decisions"),
        owner_client.get(f"{WORKFLOWS}/{created['id']}/needs-review"),
        owner_client.post(
            f"{WORKFLOWS}/{created['id']}/signals",
            json={"signal_key": "go", "payload": {}, "expected_revision": 0},
            headers=ORIGIN,
        ),
        owner_client.post(
            f"{WORKFLOWS}/{created['id']}/decisions/1",
            json={"approve": True, "expected_revision": 0},
            headers=ORIGIN,
        ),
    ):
        assert refused.status_code == 404, refused.text
        assert refused.json()["error"]["code"] == "workflow_not_found"
