"""Two-provider portability proofs at the authenticated API surface.

C2 makes the control plane capability-blind: it accepts a Run for any provider identifier
NervOS knows, and the durable Job records which provider must execute it. These tests prove
that the second provider is a *configuration choice of the same acceptance path* — no
fallback, no second route, and a per-Run immutable snapshot — while execution routing itself
is proven in the Worker suite, where the provider call actually happens.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from nervos_core.application.model_providers import ModelProviderCatalog

PROVIDER_ID = "anthropic"
SECOND_PROVIDER_ID = "openai"
INSTANCES = "/api/v1/agent-instances"
ORIGIN = {"Origin": "http://localhost:5173"}


class RecordingCredentialFreeCompletion:
    """A double that must never be called by the control plane."""

    def __init__(self, provider_id: str, model_name: str) -> None:
        self.provider_id = provider_id
        self.model_name = model_name
        self.calls = 0

    async def complete(self, request: Any) -> Any:  # pragma: no cover - must stay uncalled
        del request
        self.calls += 1
        raise AssertionError("the control plane must never invoke a model provider")


def two_provider_catalog(
    anthropic: RecordingCredentialFreeCompletion, openai: RecordingCredentialFreeCompletion
) -> ModelProviderCatalog:
    """Return a catalog configuring both providers with distinct, never-invoked doubles."""
    return ModelProviderCatalog(
        [(PROVIDER_ID, lambda: anthropic), (SECOND_PROVIDER_ID, lambda: openai)],
        known=[PROVIDER_ID, SECOND_PROVIDER_ID],
    )


def bare_catalog() -> ModelProviderCatalog:
    """Return a catalog where both providers are known but neither is configured."""
    return ModelProviderCatalog([], known=[PROVIDER_ID, SECOND_PROVIDER_ID])


def create_instance(client: TestClient, provider: str, model: str) -> dict[str, Any]:
    response = client.post(
        INSTANCES,
        json={
            "agent_key": "nervos.chat",
            "agent_definition_version": "1",
            "display_name": "Two provider",
            "model_provider": provider,
            "model_name": model,
        },
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    return response.json()


def submit(client: TestClient, instance_id: int, prompt: str = "hello") -> Any:
    return client.post(f"{INSTANCES}/{instance_id}/runs", json={"input": prompt}, headers=ORIGIN)


def test_both_providers_are_known_and_neither_is_a_fallback(
    owner_client: TestClient, install_provider_catalog: Callable[..., None]
) -> None:
    anthropic = RecordingCredentialFreeCompletion(PROVIDER_ID, "opaque/anthropic-model")
    openai = RecordingCredentialFreeCompletion(SECOND_PROVIDER_ID, "opaque/openai-model")
    install_provider_catalog(two_provider_catalog(anthropic, openai))

    created = create_instance(owner_client, SECOND_PROVIDER_ID, "opaque/openai-model")

    assert created["model_provider"] == SECOND_PROVIDER_ID
    response = submit(owner_client, created["id"])
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "created"
    assert response.json()["model_provider"] == SECOND_PROVIDER_ID
    # Acceptance never touches a provider, so neither double was invoked or resolved away.
    assert anthropic.calls == 0 and openai.calls == 0


def test_an_unknown_third_provider_is_still_rejected(
    owner_client: TestClient, install_provider_catalog: Callable[..., None]
) -> None:
    install_provider_catalog(bare_catalog())
    response = owner_client.post(
        INSTANCES,
        json={
            "agent_key": "nervos.chat",
            "agent_definition_version": "1",
            "display_name": "Unknown",
            "model_provider": "not-a-provider",
            "model_name": "opaque/model",
        },
        headers=ORIGIN,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_model_provider"


def test_a_known_but_locally_unconfigured_provider_is_accepted(
    owner_client: TestClient, install_provider_catalog: Callable[..., None]
) -> None:
    """C2 behaviour change: execution capability belongs to the Worker, not to admission.

    A known provider whose credential this process does not hold is accepted and waits in the
    durable queue until a Worker capable of it exists. Rejecting it here would make the
    control plane's credential inventory a silent admission policy.
    """
    install_provider_catalog(bare_catalog())
    created = create_instance(owner_client, SECOND_PROVIDER_ID, "opaque/openai-model")

    response = submit(owner_client, created["id"])

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "created"
    assert body["model_provider"] == SECOND_PROVIDER_ID
    assert body["output_text"] is None and body["error_code"] is None


def test_each_run_keeps_its_own_immutable_provider_snapshot(
    owner_client: TestClient, install_provider_catalog: Callable[..., None]
) -> None:
    anthropic = RecordingCredentialFreeCompletion(PROVIDER_ID, "opaque/anthropic-model")
    openai = RecordingCredentialFreeCompletion(SECOND_PROVIDER_ID, "opaque/openai-model")
    install_provider_catalog(two_provider_catalog(anthropic, openai))
    created = create_instance(owner_client, PROVIDER_ID, "opaque/anthropic-model")

    first = submit(owner_client, created["id"]).json()
    patched = owner_client.patch(
        f"{INSTANCES}/{created['id']}",
        json={
            "display_name": created["display_name"],
            "model_provider": SECOND_PROVIDER_ID,
            "model_name": "opaque/openai-model",
        },
        headers=ORIGIN,
    )
    assert patched.status_code == 200
    second = submit(owner_client, created["id"]).json()

    reread = owner_client.get(f"/api/v1/runs/{first['id']}", headers=ORIGIN).json()

    assert first["model_provider"] == PROVIDER_ID
    assert first["model_name"] == "opaque/anthropic-model"
    assert second["model_provider"] == SECOND_PROVIDER_ID
    assert second["model_name"] == "opaque/openai-model"
    assert reread == first
    assert first["id"] != second["id"]
