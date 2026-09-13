"""Owner-scoped Agent Instance HTTP API tests."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from nervos_core.application.model_providers import ModelProviderCatalog
from nervos_core.domain.agents import AgentInstance

INSTANCES = "/api/v1/agent-instances"
ORIGIN = {"Origin": "http://localhost:5173"}
PROVIDER_ID = "anthropic"


def instance_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "agent_key": "nervos.chat",
        "agent_definition_version": "1",
        "display_name": "Chat",
        "model_provider": PROVIDER_ID,
        "model_name": "opaque/model",
    }
    body.update(overrides)
    return body


def created_instance(client: TestClient, **overrides: Any) -> dict[str, Any]:
    response = client.post(INSTANCES, json=instance_body(**overrides), headers=ORIGIN)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", INSTANCES, None),
        ("POST", INSTANCES, {}),
        ("GET", f"{INSTANCES}/1", None),
        ("PATCH", f"{INSTANCES}/1", {}),
        ("POST", f"{INSTANCES}/1/runs", {"input": "hello"}),
        ("GET", f"{INSTANCES}/1/runs", None),
        ("GET", "/api/v1/runs/1", None),
    ],
)
def test_every_b3_route_requires_authentication(
    client: TestClient, method: str, path: str, body: dict[str, Any] | None
) -> None:
    response = client.request(method, path, json=body, headers=ORIGIN)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


def test_mutations_require_the_exact_origin(owner_client: TestClient) -> None:
    for method, path, body in (
        ("POST", INSTANCES, instance_body()),
        ("PATCH", f"{INSTANCES}/1", {"enabled": False}),
    ):
        missing = owner_client.request(method, path, json=body)
        wrong = owner_client.request(
            method, path, json=body, headers={"Origin": "http://evil.test"}
        )

        assert missing.status_code == 403
        assert missing.json()["error"]["code"] == "invalid_origin"
        assert wrong.status_code == 403


def test_mutations_reject_a_non_json_content_type(owner_client: TestClient) -> None:
    response = owner_client.post(
        INSTANCES,
        content="agent_key=nervos.chat",
        headers={**ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"


def test_mutations_reject_an_oversized_body(owner_client: TestClient) -> None:
    oversized = b"{" + b"a" * (17 * 1024) + b"}"

    response = owner_client.post(
        INSTANCES, content=oversized, headers={**ORIGIN, "Content-Type": "application/json"}
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


def test_create_returns_a_safe_flattened_instance(owner_client: TestClient) -> None:
    body = created_instance(owner_client)

    assert body == {
        "id": 1,
        "agent_key": "nervos.chat",
        "agent_definition_version": "1",
        "display_name": "Chat",
        "enabled": True,
        "model_provider": PROVIDER_ID,
        "model_name": "opaque/model",
        "created_at": body["created_at"],
        "updated_at": body["updated_at"],
    }
    assert "owner_user_id" not in body


@pytest.mark.parametrize(
    ("agent_key", "version"),
    [
        ("nervos.chat", "2"),
        ("nervos.chat", "latest"),
        ("other.agent", "1"),
        ("nervos.other", "1"),
    ],
)
def test_creation_rejects_any_definition_outside_this_milestone(
    owner_client: TestClient, agent_key: str, version: str
) -> None:
    response = owner_client.post(
        INSTANCES,
        json=instance_body(agent_key=agent_key, agent_definition_version=version),
        headers=ORIGIN,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_agent_definition"
    assert owner_client.get(INSTANCES, headers=ORIGIN).json()["items"] == []


@pytest.mark.parametrize(
    ("agent_key", "version"), [("", "1"), ("Bad Key", "1"), ("nervos.chat", "")]
)
def test_creation_rejects_a_malformed_definition_identity(
    owner_client: TestClient, agent_key: str, version: str
) -> None:
    response = owner_client.post(
        INSTANCES,
        json=instance_body(agent_key=agent_key, agent_definition_version=version),
        headers=ORIGIN,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_agent_definition"
    assert owner_client.get(INSTANCES, headers=ORIGIN).json()["items"] == []


def test_creation_rejects_an_unknown_provider(owner_client: TestClient) -> None:
    response = owner_client.post(
        INSTANCES, json=instance_body(model_provider="not-a-provider"), headers=ORIGIN
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_model_provider"
    assert owner_client.get(INSTANCES, headers=ORIGIN).json()["items"] == []


def test_creation_accepts_a_known_but_unconfigured_provider(
    owner_client: TestClient,
    install_provider_catalog: Callable[[ModelProviderCatalog], None],
    unconfigured_provider_catalog: ModelProviderCatalog,
) -> None:
    install_provider_catalog(unconfigured_provider_catalog)

    response = owner_client.post(INSTANCES, json=instance_body(), headers=ORIGIN)

    assert response.status_code == 201
    assert response.json()["model_provider"] == PROVIDER_ID


@pytest.mark.parametrize(
    "overrides",
    [
        {"display_name": ""},
        {"display_name": "   "},
        {"display_name": "bad\nname"},
        {"model_name": ""},
        {"model_name": "bad\nmodel"},
        {"model_provider": ""},
    ],
)
def test_creation_rejects_invalid_configuration(
    owner_client: TestClient, overrides: dict[str, str]
) -> None:
    response = owner_client.post(INSTANCES, json=instance_body(**overrides), headers=ORIGIN)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_agent_instance"
    assert owner_client.get(INSTANCES, headers=ORIGIN).json()["items"] == []


def test_creation_never_accepts_a_client_supplied_owner(owner_client: TestClient) -> None:
    response = owner_client.post(
        INSTANCES, json={**instance_body(), "owner_user_id": 99}, headers=ORIGIN
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_list_returns_newest_first_with_a_cursor(owner_client: TestClient) -> None:
    first = created_instance(owner_client, display_name="First")
    second = created_instance(owner_client, display_name="Second")

    page = owner_client.get(INSTANCES, headers=ORIGIN).json()
    assert [item["id"] for item in page["items"]] == [second["id"], first["id"]]
    assert page["next_before_id"] is None

    limited = owner_client.get(f"{INSTANCES}?limit=1", headers=ORIGIN).json()
    assert [item["id"] for item in limited["items"]] == [second["id"]]
    assert limited["next_before_id"] == second["id"]

    older = owner_client.get(f"{INSTANCES}?limit=1&before_id={second['id']}", headers=ORIGIN).json()
    assert [item["id"] for item in older["items"]] == [first["id"]]


@pytest.mark.parametrize("query", ["limit=0", "limit=51", "limit=abc", "before_id=0"])
def test_list_rejects_invalid_pagination(owner_client: TestClient, query: str) -> None:
    response = owner_client.get(f"{INSTANCES}?{query}", headers=ORIGIN)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_get_returns_the_owned_instance(owner_client: TestClient) -> None:
    created = created_instance(owner_client)

    response = owner_client.get(f"{INSTANCES}/{created['id']}", headers=ORIGIN)

    assert response.status_code == 200
    assert response.json() == created


def test_foreign_and_nonexistent_instances_are_indistinguishable(
    owner_client: TestClient,
    seed_user_account: Callable[[str], int],
    sign_in_as: Callable[[str], None],
) -> None:
    created = created_instance(owner_client)
    seed_user_account("intruder")
    sign_in_as("intruder")

    foreign = owner_client.get(f"{INSTANCES}/{created['id']}", headers=ORIGIN)
    missing = owner_client.get(f"{INSTANCES}/9999", headers=ORIGIN)

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert foreign.json()["error"]["code"] == "agent_instance_not_found"

    foreign_patch = owner_client.patch(
        f"{INSTANCES}/{created['id']}", json={"enabled": False}, headers=ORIGIN
    )
    assert foreign_patch.status_code == 404
    assert owner_client.get(INSTANCES, headers=ORIGIN).json()["items"] == []

    sign_in_as("owner")
    restored = owner_client.get(f"{INSTANCES}/{created['id']}", headers=ORIGIN).json()
    assert restored["enabled"] is True


def test_configuration_patch_is_one_committed_mutation(
    owner_client: TestClient, app_under_test: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = created_instance(owner_client)
    service = app_under_test.state.agent_service
    updates: list[tuple[Any, ...]] = []
    toggles: list[tuple[Any, ...]] = []
    original_update = service.update_instance
    original_toggle = service.set_enabled

    def record_update(
        owner_user_id: int,
        instance_id: int,
        display_name: str,
        model_provider: str,
        model_name: str,
    ) -> AgentInstance:
        updates.append((owner_user_id, instance_id, display_name, model_provider, model_name))
        return original_update(owner_user_id, instance_id, display_name, model_provider, model_name)

    def record_toggle(owner_user_id: int, instance_id: int, enabled: bool) -> AgentInstance:
        toggles.append((owner_user_id, instance_id, enabled))
        return original_toggle(owner_user_id, instance_id, enabled)

    monkeypatch.setattr(service, "update_instance", record_update)
    monkeypatch.setattr(service, "set_enabled", record_toggle)

    response = owner_client.patch(
        f"{INSTANCES}/{created['id']}",
        json={
            "display_name": "Renamed",
            "model_provider": PROVIDER_ID,
            "model_name": "opaque/other-model",
        },
        headers=ORIGIN,
    )

    assert response.status_code == 200, response.text
    assert response.json()["display_name"] == "Renamed"
    assert response.json()["model_name"] == "opaque/other-model"
    assert len(updates) == 1
    assert toggles == []


def test_enable_patch_is_one_committed_mutation(
    owner_client: TestClient, app_under_test: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = created_instance(owner_client)
    service = app_under_test.state.agent_service
    updates: list[tuple[Any, ...]] = []
    toggles: list[tuple[Any, ...]] = []
    original_update = service.update_instance
    original_toggle = service.set_enabled

    def record_update(
        owner_user_id: int,
        instance_id: int,
        display_name: str,
        model_provider: str,
        model_name: str,
    ) -> AgentInstance:
        updates.append((owner_user_id, instance_id, display_name, model_provider, model_name))
        return original_update(owner_user_id, instance_id, display_name, model_provider, model_name)

    def record_toggle(owner_user_id: int, instance_id: int, enabled: bool) -> AgentInstance:
        toggles.append((owner_user_id, instance_id, enabled))
        return original_toggle(owner_user_id, instance_id, enabled)

    monkeypatch.setattr(service, "update_instance", record_update)
    monkeypatch.setattr(service, "set_enabled", record_toggle)

    disabled = owner_client.patch(
        f"{INSTANCES}/{created['id']}", json={"enabled": False}, headers=ORIGIN
    )
    enabled = owner_client.patch(
        f"{INSTANCES}/{created['id']}", json={"enabled": True}, headers=ORIGIN
    )

    assert disabled.status_code == 200 and disabled.json()["enabled"] is False
    assert enabled.status_code == 200 and enabled.json()["enabled"] is True
    assert len(toggles) == 2
    assert updates == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"display_name": "Only a name"},
        {"model_provider": PROVIDER_ID, "model_name": "opaque/model"},
        {
            "display_name": "Mixed",
            "model_provider": PROVIDER_ID,
            "model_name": "m",
            "enabled": True,
        },
        {"enabled": True, "display_name": "Mixed"},
        {"agent_key": "nervos.chat"},
        {"agent_definition_version": "1"},
        {"owner_user_id": 1},
    ],
)
def test_patch_rejects_every_disallowed_shape(
    owner_client: TestClient, body: dict[str, Any]
) -> None:
    created = created_instance(owner_client)

    response = owner_client.patch(f"{INSTANCES}/{created['id']}", json=body, headers=ORIGIN)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"


def test_patch_never_replaces_an_explicitly_empty_value(owner_client: TestClient) -> None:
    created = created_instance(owner_client, display_name="Original")

    response = owner_client.patch(
        f"{INSTANCES}/{created['id']}",
        json={"display_name": "", "model_provider": PROVIDER_ID, "model_name": "opaque/model"},
        headers=ORIGIN,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_agent_instance"
    stored = owner_client.get(f"{INSTANCES}/{created['id']}", headers=ORIGIN).json()
    assert stored["display_name"] == "Original"


def test_patch_rejects_an_unknown_provider_without_persisting_it(owner_client: TestClient) -> None:
    created = created_instance(owner_client)

    response = owner_client.patch(
        f"{INSTANCES}/{created['id']}",
        json={"display_name": "Chat", "model_provider": "not-a-provider", "model_name": "m"},
        headers=ORIGIN,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_model_provider"
    stored = owner_client.get(f"{INSTANCES}/{created['id']}", headers=ORIGIN).json()
    assert stored["model_provider"] == PROVIDER_ID
    assert stored["model_name"] == "opaque/model"


def test_patch_accepts_a_known_but_unconfigured_provider(
    owner_client: TestClient,
    install_provider_catalog: Callable[[ModelProviderCatalog], None],
    unconfigured_provider_catalog: ModelProviderCatalog,
) -> None:
    created = created_instance(owner_client)
    install_provider_catalog(unconfigured_provider_catalog)

    response = owner_client.patch(
        f"{INSTANCES}/{created['id']}",
        json={
            "display_name": "Chat",
            "model_provider": PROVIDER_ID,
            "model_name": "opaque/another-model",
        },
        headers=ORIGIN,
    )

    assert response.status_code == 200
    assert response.json()["model_name"] == "opaque/another-model"


def test_disable_and_reenable_keep_the_instance_visible(owner_client: TestClient) -> None:
    created = created_instance(owner_client)

    owner_client.patch(f"{INSTANCES}/{created['id']}", json={"enabled": False}, headers=ORIGIN)

    listed = owner_client.get(INSTANCES, headers=ORIGIN).json()["items"]
    assert [item["id"] for item in listed] == [created["id"]]
    assert listed[0]["enabled"] is False


def test_no_delete_route_is_exposed(owner_client: TestClient) -> None:
    created = created_instance(owner_client)

    response = owner_client.delete(f"{INSTANCES}/{created['id']}", headers=ORIGIN)

    assert response.status_code == 405


def test_instance_routes_never_create_a_run(owner_client: TestClient) -> None:
    created = created_instance(owner_client)

    owner_client.get(INSTANCES, headers=ORIGIN)
    owner_client.get(f"{INSTANCES}/{created['id']}", headers=ORIGIN)
    owner_client.patch(f"{INSTANCES}/{created['id']}", json={"enabled": True}, headers=ORIGIN)

    assert (
        owner_client.get(f"{INSTANCES}/{created['id']}/runs", headers=ORIGIN).json()["items"] == []
    )


@pytest.mark.parametrize(
    "body",
    [
        {"display_name": "   ", "model_provider": PROVIDER_ID, "model_name": "opaque/model"},
        {"display_name": "Chat", "model_provider": PROVIDER_ID, "model_name": ""},
        {"display_name": "Chat", "model_provider": PROVIDER_ID, "model_name": "   "},
        {"display_name": "Chat", "model_provider": "", "model_name": "opaque/model"},
        {"display_name": "Chat", "model_provider": "   ", "model_name": "opaque/model"},
    ],
)
def test_patch_sends_every_empty_value_to_domain_validation(
    owner_client: TestClient, body: dict[str, Any]
) -> None:
    """An explicitly supplied blank value must reach B1, never be replaced by the stored one."""
    created = created_instance(
        owner_client, display_name="Original", model_name="opaque/original-model"
    )

    response = owner_client.patch(f"{INSTANCES}/{created['id']}", json=body, headers=ORIGIN)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_agent_instance"
    stored = owner_client.get(f"{INSTANCES}/{created['id']}", headers=ORIGIN).json()
    assert stored["display_name"] == "Original"
    assert stored["model_name"] == "opaque/original-model"
    assert stored["model_provider"] == PROVIDER_ID


def test_authenticated_responses_are_never_cacheable(owner_client: TestClient) -> None:
    """Prompts and model answers must not be recoverable from a browser cache."""
    created = created_instance(owner_client)

    responses = (
        owner_client.get(INSTANCES, headers=ORIGIN),
        owner_client.get(f"{INSTANCES}/{created['id']}", headers=ORIGIN),
        owner_client.get(f"{INSTANCES}/{created['id']}/runs", headers=ORIGIN),
    )

    for response in responses:
        assert response.headers["cache-control"] == "no-store", response.request.url


def test_a_trailing_slash_cannot_bypass_the_body_bound(
    owner_client: TestClient, deterministic_completion: Any
) -> None:
    """A path variant that is not the canonical route must still be size-guarded."""
    response = owner_client.post(
        f"{INSTANCES}/",
        content=b'{"agent_key": "' + b"a" * (17 * 1024) + b'"}',
        headers={**ORIGIN, "Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert deterministic_completion.calls == 0


def test_creation_never_returns_provider_internals(owner_client: TestClient) -> None:
    text = owner_client.post(INSTANCES, json=instance_body(), headers=ORIGIN).text.lower()

    assert "api_key" not in text
    assert "credential" not in text
    assert "secret" not in text
