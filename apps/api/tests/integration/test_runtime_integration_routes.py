"""Owner/origin protected integration product controls."""

from collections.abc import Callable

from fastapi.testclient import TestClient

ORIGIN = {"Origin": "http://localhost:5173"}


def _create(client: TestClient) -> int:
    response = client.post(
        "/api/v1/agent-instances",
        headers=ORIGIN,
        json={
            "agent_key": "nervos.chat",
            "agent_definition_version": "1",
            "display_name": "Memory agent",
            "model_provider": "anthropic",
            "model_name": "offline-model",
        },
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def test_integration_requires_authentication(client: TestClient) -> None:
    for path in (
        "/runtime-health",
        "/tools",
        "/memory-suggestions",
        "/agent-instances/1/tools",
        "/agent-instances/1/memory-policy",
        "/runs/1/context",
    ):
        assert client.get("/api/v1" + path).status_code == 401


def test_memory_policy_default_update_stale_and_validation(owner_client: TestClient) -> None:
    instance = _create(owner_client)
    path = f"/api/v1/agent-instances/{instance}/memory-policy"
    assert owner_client.get(path).json() == {
        "mode": "manual",
        "revision": 0,
        "extraction_enabled": False,
    }
    body = {"mode": "automatic_private", "extraction_enabled": True, "expected_revision": 0}
    assert owner_client.patch(path, json=body).status_code == 403
    response = owner_client.patch(path, headers=ORIGIN, json=body)
    assert response.status_code == 200
    assert response.json()["revision"] == 1
    assert owner_client.patch(path, headers=ORIGIN, json=body).status_code == 409
    body["mode"] = "manual"
    assert owner_client.patch(path, headers=ORIGIN, json=body).status_code == 422


def test_tools_health_and_context_are_safe_owner_projections(
    owner_client: TestClient,
    seed_user_account: Callable[[str], int],
    sign_in_as: Callable[[str], None],
) -> None:
    instance = _create(owner_client)
    run = owner_client.post(
        f"/api/v1/agent-instances/{instance}/runs", headers=ORIGIN, json={"input": "Hello"}
    )
    assert run.status_code == 202, run.text
    run_id = run.json()["id"]
    context = owner_client.get(f"/api/v1/runs/{run_id}/context")
    assert context.status_code == 200 and context.json()["current_input"] == "Hello"
    assert owner_client.get(f"/api/v1/agent-instances/{instance}/tools").status_code == 200
    health = owner_client.get("/api/v1/runtime-health")
    assert set(health.json()) == {
        "observed_at",
        "execution_available",
        "owner_jobs",
        "package_sandbox",
    }
    assert health.json()["package_sandbox"]["supported"] is False
    assert isinstance(health.json()["package_sandbox"]["platform"], str)
    assert "path" not in health.json()["package_sandbox"]
    assert owner_client.get("/api/v1/memory-suggestions").json()["items"] == []
    seed_user_account("other")
    sign_in_as("other")
    for path in (
        f"/agent-instances/{instance}/tools",
        f"/agent-instances/{instance}/memory-policy",
        f"/runs/{run_id}/context",
    ):
        assert owner_client.get("/api/v1" + path).status_code == 404
    assert owner_client.get("/api/v1/runtime-health").json()["owner_jobs"] == {}
