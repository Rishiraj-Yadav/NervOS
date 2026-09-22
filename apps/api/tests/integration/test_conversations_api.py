"""Stage F1 Conversation management HTTP surface: routes, authority, messages, turns, and retry."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

ORIGIN: dict[str, str] = {"Origin": "http://localhost:5173"}
PROVIDER_ID: str = "anthropic"

CONVERSATIONS = "/api/v1/conversations"
INSTANCES = "/api/v1/agent-instances"


def make_instance(client: TestClient, name: str = "Chat") -> int:
    response = client.post(
        INSTANCES,
        json={
            "agent_key": "nervos.chat",
            "agent_definition_version": "1",
            "display_name": name,
            "model_provider": PROVIDER_ID,
            "model_name": "opaque/model",
        },
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def create_conv(
    client: TestClient, agent_instance_id: int, title: str | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"agent_instance_id": agent_instance_id}
    if title is not None:
        body["title"] = title
    response = client.post(CONVERSATIONS, json=body, headers=ORIGIN)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def agent(owner_client: TestClient) -> int:
    return make_instance(owner_client)


# ------------------------------------------------------------------------------------------------
# Authority & Authentication
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", CONVERSATIONS, None),
        ("POST", CONVERSATIONS, {"agent_instance_id": 1}),
        ("GET", f"{CONVERSATIONS}/1", None),
        ("POST", f"{CONVERSATIONS}/1/messages", {"client_message_id": "c1", "content": "hi"}),
        ("GET", f"{CONVERSATIONS}/1/turns", None),
        ("POST", f"{CONVERSATIONS}/1/turns/1/retry", None),
    ],
)
def test_every_conversation_route_requires_authentication(
    client: TestClient, method: str, path: str, body: dict[str, Any] | None
) -> None:
    response = client.request(method, path, json=body, headers=ORIGIN)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


def test_mutations_require_exact_origin(owner_client: TestClient, agent: int) -> None:
    conv = create_conv(owner_client, agent, title="Origin Test")

    # Missing origin on send message
    resp_missing = owner_client.post(
        f"{CONVERSATIONS}/{conv['id']}/messages",
        json={"client_message_id": "c1", "content": "hi"},
    )
    assert resp_missing.status_code == 403
    assert resp_missing.json()["error"]["code"] == "invalid_origin"


# ------------------------------------------------------------------------------------------------
# CRUD & Pagination
# ------------------------------------------------------------------------------------------------


def test_create_and_get_conversation_api(owner_client: TestClient, agent: int) -> None:
    conv = create_conv(owner_client, agent, title="My Conversation")
    assert conv["id"] > 0
    assert conv["title"] == "My Conversation"
    assert conv["agent_instance_id"] == agent

    get_resp = owner_client.get(f"{CONVERSATIONS}/{conv['id']}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == conv["id"]
    assert get_resp.json()["title"] == "My Conversation"


def test_list_conversations_api(owner_client: TestClient, agent: int) -> None:
    c1 = create_conv(owner_client, agent, title="Conv 1")
    c2 = create_conv(owner_client, agent, title="Conv 2")

    list_resp = owner_client.get(CONVERSATIONS)
    assert list_resp.status_code == 200
    items = list_resp.json()["items"]
    assert len(items) >= 2
    ids = [i["id"] for i in items]
    assert c2["id"] in ids
    assert c1["id"] in ids


# ------------------------------------------------------------------------------------------------
# Send Message, Idempotency & Turn Listing
# ------------------------------------------------------------------------------------------------


def test_send_message_and_idempotent_replay(owner_client: TestClient, agent: int) -> None:
    conv = create_conv(owner_client, agent)

    # Initial send -> 201 Created
    send_resp = owner_client.post(
        f"{CONVERSATIONS}/{conv['id']}/messages",
        json={"client_message_id": "msg-uuid-1", "content": "What is 2+2?"},
        headers=ORIGIN,
    )
    assert send_resp.status_code == 201, send_resp.text
    turn = send_resp.json()
    assert turn["sequence"] == 1
    assert turn["state"] == "running"
    assert turn["user_message"]["content"] == "What is 2+2?"
    assert turn["assistant_message"] is None

    # Idempotent replay same ID + same content -> 200 OK
    replay_resp = owner_client.post(
        f"{CONVERSATIONS}/{conv['id']}/messages",
        json={"client_message_id": "msg-uuid-1", "content": "What is 2+2?"},
        headers=ORIGIN,
    )
    assert replay_resp.status_code == 200
    assert replay_resp.json()["id"] == turn["id"]

    # Same ID + different content -> 409 Conflict
    conflict_resp = owner_client.post(
        f"{CONVERSATIONS}/{conv['id']}/messages",
        json={"client_message_id": "msg-uuid-1", "content": "What is 3+3?"},
        headers=ORIGIN,
    )
    assert conflict_resp.status_code == 409
    assert conflict_resp.json()["error"]["code"] == "conversation_conflict"

    # Different ID while turn is running -> 409 Busy
    busy_resp = owner_client.post(
        f"{CONVERSATIONS}/{conv['id']}/messages",
        json={"client_message_id": "msg-uuid-2", "content": "Another message"},
        headers=ORIGIN,
    )
    assert busy_resp.status_code == 409
    assert busy_resp.json()["error"]["code"] == "conversation_busy"


def test_list_turns_history_api(owner_client: TestClient, agent: int) -> None:
    conv = create_conv(owner_client, agent)
    owner_client.post(
        f"{CONVERSATIONS}/{conv['id']}/messages",
        json={"client_message_id": "msg-uuid-1", "content": "Turn 1"},
        headers=ORIGIN,
    )

    turns_resp = owner_client.get(f"{CONVERSATIONS}/{conv['id']}/turns")
    assert turns_resp.status_code == 200
    turns = turns_resp.json()["items"]
    assert len(turns) == 1
    assert turns[0]["sequence"] == 1
    assert turns[0]["user_message"]["content"] == "Turn 1"


# ------------------------------------------------------------------------------------------------
# IDOR & Isolation Collapse
# ------------------------------------------------------------------------------------------------


def test_cross_owner_isolation_collapse_api(
    owner_client: TestClient,
    client: TestClient,
    agent: int,
    seed_user_account: Any,
    sign_in_as: Any,
) -> None:
    conv = create_conv(owner_client, agent, title="Owner 1 Only")

    # Seed and sign in as other owner
    seed_user_account("other_owner")
    sign_in_as("other_owner")

    # Accessing owner 1's conversation yields 404 (not 403)
    get_resp = client.get(f"{CONVERSATIONS}/{conv['id']}")
    assert get_resp.status_code == 404
    assert get_resp.json()["error"]["code"] == "conversation_not_found"

    # List returns empty for other owner
    list_resp = client.get(CONVERSATIONS)
    assert list_resp.status_code == 200
    assert list_resp.json()["items"] == []
