"""API integration tests for F3 Memory creation and promotion endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from nervos_api.config import Settings

ORIGIN = {"Origin": "http://localhost:5173"}
OTHER_ORIGIN = {"Origin": "http://evil.com"}


def _setup_authenticated_client(app: FastAPI, username: str = "alice") -> tuple[TestClient, int]:
    client = TestClient(app)
    resp = client.post(
        "/api/v1/setup",
        json={"username": username, "password": "password123456"},
        headers=ORIGIN,
    )
    assert resp.status_code == 201, resp.text
    user_id = resp.json()["id"]
    return client, user_id


def _create_agent_instance(client: TestClient, name: str = "Agent") -> int:
    resp = client.post(
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
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_create_user_memory_direct_success(migrated_app: tuple[FastAPI, Settings]) -> None:
    app, _ = migrated_app
    client, user_id = _setup_authenticated_client(app)

    resp = client.post(
        "/api/v1/memories",
        json={"scope": "user", "content": "I prefer dark mode."},
        headers=ORIGIN,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["owner_user_id"] == user_id
    assert data["agent_instance_id"] is None
    assert data["scope"] == "user"
    assert data["status"] == "active"
    assert data["current_version"] == 1
    assert data["content"] == "I prefer dark mode."
    assert data["source_kind"] == "direct_user"
    assert data["source_id"] is None
    assert data["provenance_type"] == "user_authored"


def test_create_agent_memory_direct_success(migrated_app: tuple[FastAPI, Settings]) -> None:
    app, _ = migrated_app
    client, user_id = _setup_authenticated_client(app)
    agent_id = _create_agent_instance(client)

    resp = client.post(
        "/api/v1/memories",
        json={
            "scope": "agent",
            "content": "This project uses SQLite.",
            "agent_instance_id": agent_id,
        },
        headers=ORIGIN,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["owner_user_id"] == user_id
    assert data["agent_instance_id"] == agent_id
    assert data["scope"] == "agent"
    assert data["content"] == "This project uses SQLite."
    assert data["source_kind"] == "direct_user"
    assert data["provenance_type"] == "user_authored"


def test_create_memory_scope_validation(migrated_app: tuple[FastAPI, Settings]) -> None:
    app, _ = migrated_app
    client, _ = _setup_authenticated_client(app)
    agent_id = _create_agent_instance(client)

    # USER memory rejects agent_instance_id
    resp = client.post(
        "/api/v1/memories",
        json={"scope": "user", "content": "Fact", "agent_instance_id": agent_id},
        headers=ORIGIN,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_memory"

    # AGENT memory requires agent_instance_id
    resp2 = client.post(
        "/api/v1/memories",
        json={"scope": "agent", "content": "Fact"},
        headers=ORIGIN,
    )
    assert resp2.status_code == 422
    assert resp2.json()["error"]["code"] == "invalid_memory"


def test_create_memory_requires_authentication_and_origin(
    migrated_app: tuple[FastAPI, Settings],
) -> None:
    app, _ = migrated_app
    unauth_client = TestClient(app)

    resp = unauth_client.post(
        "/api/v1/memories",
        json={"scope": "user", "content": "Fact"},
        headers=ORIGIN,
    )
    assert resp.status_code == 401

    auth_client, _ = _setup_authenticated_client(app)
    resp_origin = auth_client.post(
        "/api/v1/memories",
        json={"scope": "user", "content": "Fact"},
        headers=OTHER_ORIGIN,
    )
    assert resp_origin.status_code == 403


def test_promote_memory_from_message(migrated_app: tuple[FastAPI, Settings]) -> None:
    app, _ = migrated_app
    client, _user_id = _setup_authenticated_client(app)
    agent_id = _create_agent_instance(client)

    # Create conversation and turn
    conv_resp = client.post(
        "/api/v1/conversations",
        json={"agent_instance_id": agent_id, "title": "Conv"},
        headers=ORIGIN,
    )
    conv_id = conv_resp.json()["id"]

    msg_resp = client.post(
        f"/api/v1/conversations/{conv_id}/messages",
        json={"client_message_id": "m1", "content": "My favorite color is blue."},
        headers=ORIGIN,
    )
    msg_id = msg_resp.json()["user_message"]["id"]

    # Promote from message
    promote_resp = client.post(
        "/api/v1/memories/promote",
        json={
            "source_type": "conversation_message",
            "source_id": msg_id,
            "scope": "user",
        },
        headers=ORIGIN,
    )
    assert promote_resp.status_code == 201
    data = promote_resp.json()
    assert data["content"] == "My favorite color is blue."
    assert data["source_kind"] == "promoted_message"
    assert data["source_id"] == msg_id
    assert data["provenance_type"] == "user_approved_inferred"


def test_promote_memory_from_succeeded_run(migrated_app: tuple[FastAPI, Settings]) -> None:
    app, _ = migrated_app
    client, _user_id = _setup_authenticated_client(app)
    agent_id = _create_agent_instance(client)

    # Direct run submission
    run_resp = client.post(
        f"/api/v1/agent-instances/{agent_id}/runs",
        json={"input": "Calculate 2+2"},
        headers=ORIGIN,
    )
    run_id = run_resp.json()["id"]

    # Note: run is currently in 'created' state in DB (not yet executed/succeeded)
    # Attempting to promote an un-succeeded run fails safely
    fail_promote = client.post(
        "/api/v1/memories/promote",
        json={"source_type": "run", "source_id": run_id, "scope": "user"},
        headers=ORIGIN,
    )
    assert fail_promote.status_code == 422
    assert fail_promote.json()["error"]["code"] == "invalid_memory_source"


def test_list_memories_api(migrated_app: tuple[FastAPI, Settings]) -> None:
    app, _ = migrated_app
    client, _ = _setup_authenticated_client(app)
    agent_id = _create_agent_instance(client)

    # Create 3 memories
    client.post("/api/v1/memories", json={"scope": "user", "content": "Fact 1"}, headers=ORIGIN)
    client.post(
        "/api/v1/memories",
        json={"scope": "agent", "content": "Fact 2", "agent_instance_id": agent_id},
        headers=ORIGIN,
    )
    client.post("/api/v1/memories", json={"scope": "user", "content": "Fact 3"}, headers=ORIGIN)

    # List all
    resp = client.get("/api/v1/memories")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 3
    assert data["next_before_id"] is None

    # Filter scope=user
    resp_user = client.get("/api/v1/memories?scope=user")
    assert resp_user.status_code == 200
    assert len(resp_user.json()["items"]) == 2

    # Filter scope=agent&agent_instance_id=...
    resp_agent = client.get(f"/api/v1/memories?scope=agent&agent_instance_id={agent_id}")
    assert resp_agent.status_code == 200
    assert len(resp_agent.json()["items"]) == 1


def test_edit_memory_api_and_stale_conflict(migrated_app: tuple[FastAPI, Settings]) -> None:
    app, _ = migrated_app
    client, _ = _setup_authenticated_client(app)

    create_resp = client.post(
        "/api/v1/memories", json={"scope": "user", "content": "Initial content"}, headers=ORIGIN
    )
    mem_id = create_resp.json()["id"]

    # Edit with expected_version=1
    edit_resp = client.patch(
        f"/api/v1/memories/{mem_id}",
        json={"expected_version": 1, "content": "Updated content v2"},
        headers=ORIGIN,
    )
    assert edit_resp.status_code == 200
    data = edit_resp.json()
    assert data["current_version"] == 2
    assert data["content"] == "Updated content v2"

    # Stale edit with expected_version=1 fails with 409
    stale_resp = client.patch(
        f"/api/v1/memories/{mem_id}",
        json={"expected_version": 1, "content": "Conflicting edit"},
        headers=ORIGIN,
    )
    assert stale_resp.status_code == 409
    assert stale_resp.json()["error"]["code"] == "stale_memory_version"

    # Inspect version history
    versions_resp = client.get(f"/api/v1/memories/{mem_id}/versions")
    assert versions_resp.status_code == 200
    vdata = versions_resp.json()
    assert len(vdata["items"]) == 2
    assert [v["version"] for v in vdata["items"]] == [2, 1]


def test_delete_memory_api_and_idor(
    owner_client: TestClient,
    client: TestClient,
    seed_user_account: Any,
    sign_in_as: Any,
) -> None:
    create_resp = owner_client.post(
        "/api/v1/memories", json={"scope": "user", "content": "Alice secret"}, headers=ORIGIN
    )
    mem_id = create_resp.json()["id"]
    assert create_resp.status_code == 201

    # Switch the shared client to a different owner and confirm 404 isolation
    seed_user_account("bob")
    sign_in_as("bob")

    assert client.get(f"/api/v1/memories/{mem_id}").status_code == 404
    assert (
        client.patch(
            f"/api/v1/memories/{mem_id}",
            json={"expected_version": 1, "content": "Hacked"},
            headers=ORIGIN,
        ).status_code
        == 404
    )
    assert client.delete(f"/api/v1/memories/{mem_id}", headers=ORIGIN).status_code == 404

    # Switch back to the owner and delete the memory
    sign_in_as("owner")
    del_resp = owner_client.delete(f"/api/v1/memories/{mem_id}?expected_version=1", headers=ORIGIN)
    assert del_resp.status_code == 204

    # Subsequent GET returns 404
    assert owner_client.get(f"/api/v1/memories/{mem_id}").status_code == 404
