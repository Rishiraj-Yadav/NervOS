"""Two-provider portability proofs at the authenticated B3 API surface.

These tests use the existing seven B3 operations only. They prove that the second
provider is a configuration choice of the same execution path, never a fallback.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from nervos_core.application.model_completion import (
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    StopOutcome,
)
from nervos_core.application.model_providers import ModelProviderCatalog

PROVIDER_ID = "anthropic"
SECOND_PROVIDER_ID = "openai"
INSTANCES = "/api/v1/agent-instances"
ORIGIN = {"Origin": "http://localhost:5173"}


class RecordingCompletion:
    """Offline provider double that reports one exact canonical provider identity."""

    def __init__(self, provider_id: str, text: str) -> None:
        self.provider_id = provider_id
        self.text = text
        self.calls = 0
        self.requests: list[ModelRequest] = []
        self.error: Exception | None = None

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return ModelResponse(
            self.text,
            self.provider_id,
            request.model_name,
            StopOutcome.STOP,
            ModelUsage(11, 3, None),
        )


def instance_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "agent_key": "nervos.chat",
        "agent_definition_version": "1",
        "display_name": "Two Provider Chat",
        "model_provider": PROVIDER_ID,
        "model_name": "opaque/anthropic-model",
    }
    body.update(overrides)
    return body


def install(install_provider_catalog: Any, **completions: str) -> tuple[Any, Any]:
    """Install a two-provider catalog and return both recording doubles.

    Each canonical ID gets its own double, so a misrouted lookup or a fallback shows up
    as a call on the wrong recorder rather than as a passing test.
    """
    anthropic = RecordingCompletion(PROVIDER_ID, completions.get("anthropic", "answer a"))
    openai = RecordingCompletion(SECOND_PROVIDER_ID, completions.get("openai", "answer b"))
    install_provider_catalog(
        ModelProviderCatalog(
            [(PROVIDER_ID, lambda: anthropic), (SECOND_PROVIDER_ID, lambda: openai)],
            known=[PROVIDER_ID, SECOND_PROVIDER_ID],
        )
    )
    return anthropic, openai


def install_only_first_configured(install_provider_catalog: Any) -> RecordingCompletion:
    """Install a catalog where both IDs are known but only the first is configured."""
    anthropic = RecordingCompletion(PROVIDER_ID, "answer a")
    install_provider_catalog(
        ModelProviderCatalog(
            [(PROVIDER_ID, lambda: anthropic)], known=[PROVIDER_ID, SECOND_PROVIDER_ID]
        )
    )
    return anthropic


def execute(client: TestClient, instance_id: int, text: str = "hello") -> Any:
    return client.post(f"{INSTANCES}/{instance_id}/runs", json={"input": text}, headers=ORIGIN)


def create(client: TestClient, **overrides: Any) -> dict[str, Any]:
    response = client.post(INSTANCES, json=instance_body(**overrides), headers=ORIGIN)
    assert response.status_code == 201, response.text
    return response.json()


def test_the_second_provider_is_known_without_a_process_credential(
    owner_client: TestClient, install_provider_catalog: Any
) -> None:
    """Known-but-unconfigured stays storable; only execution is unavailable."""
    install_only_first_configured(install_provider_catalog)

    body = create(owner_client, model_provider=SECOND_PROVIDER_ID, model_name="opaque/second")

    assert body["model_provider"] == SECOND_PROVIDER_ID
    assert body["model_name"] == "opaque/second"


def test_an_unknown_third_provider_is_still_rejected(
    owner_client: TestClient, install_provider_catalog: Any
) -> None:
    install(install_provider_catalog)

    response = owner_client.post(
        INSTANCES, json=instance_body(model_provider="gemini"), headers=ORIGIN
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_model_provider"
    assert owner_client.get(INSTANCES, headers=ORIGIN).json()["items"] == []


def test_updating_configuration_to_the_second_provider_is_accepted(
    owner_client: TestClient, install_provider_catalog: Any
) -> None:
    install(install_provider_catalog)
    created = create(owner_client)

    response = owner_client.patch(
        f"{INSTANCES}/{created['id']}",
        json={
            "display_name": created["display_name"],
            "model_provider": SECOND_PROVIDER_ID,
            "model_name": "opaque/second",
        },
        headers=ORIGIN,
    )

    assert response.status_code == 200
    assert response.json()["model_provider"] == SECOND_PROVIDER_ID


def test_unconfigured_second_provider_execution_is_rejected_before_any_run(
    owner_client: TestClient, install_provider_catalog: Any
) -> None:
    anthropic = install_only_first_configured(install_provider_catalog)
    created = create(owner_client, model_provider=SECOND_PROVIDER_ID)

    response = execute(owner_client, created["id"])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "model_provider_unavailable"
    assert owner_client.get(f"{INSTANCES}/{created['id']}/runs", headers=ORIGIN).json() == {
        "items": [],
        "next_before_id": None,
    }
    assert anthropic.calls == 0


def test_second_provider_executes_through_the_unchanged_endpoint(
    owner_client: TestClient, install_provider_catalog: Any
) -> None:
    anthropic, openai = install(install_provider_catalog, openai="openai answer")
    created = create(owner_client, model_provider=SECOND_PROVIDER_ID)

    response = execute(owner_client, created["id"])

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["output_text"] == "openai answer"
    assert body["model_provider"] == SECOND_PROVIDER_ID
    assert response.headers["Location"] == f"/api/v1/runs/{body['id']}"
    assert openai.calls == 1
    assert anthropic.calls == 0


def test_a_failed_second_provider_run_is_persisted_not_a_platform_error(
    owner_client: TestClient, install_provider_catalog: Any
) -> None:
    anthropic, openai = install(install_provider_catalog)
    openai.error = ModelProviderError("model_rate_limited")
    created = create(owner_client, model_provider=SECOND_PROVIDER_ID)

    response = execute(owner_client, created["id"])

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "failed"
    assert body["error_code"] == "model_rate_limited"
    assert body["output_text"] is None
    assert anthropic.calls == 0


def test_a_failing_second_provider_never_falls_back_to_the_first(
    owner_client: TestClient, install_provider_catalog: Any
) -> None:
    """Portability is explicit configuration, so a configured peer provider stays untouched."""
    anthropic, openai = install(install_provider_catalog)
    openai.error = ModelProviderError("model_unavailable")
    created = create(owner_client, model_provider=SECOND_PROVIDER_ID)

    response = execute(owner_client, created["id"])

    assert response.status_code == 201
    assert response.json()["status"] == "failed"
    assert openai.calls == 1
    assert anthropic.calls == 0


def test_a_failing_first_provider_never_falls_back_to_the_second(
    owner_client: TestClient, install_provider_catalog: Any
) -> None:
    anthro, openai = install(install_provider_catalog)
    anthropic = anthro
    anthropic.error = ModelProviderError("model_unavailable")
    created = create(owner_client)

    response = execute(owner_client, created["id"])

    assert response.status_code == 201
    assert response.json()["status"] == "failed"
    assert anthropic.calls == 1
    assert openai.calls == 0


def test_switching_provider_leaves_existing_runs_untouched(
    owner_client: TestClient, install_provider_catalog: Any
) -> None:
    """The Run snapshot is immutable; a later configuration change affects future Runs only."""
    anthropic, openai = install(install_provider_catalog, anthropic="first answer")
    created = create(owner_client, model_name="opaque/anthropic-model")
    first = execute(owner_client, created["id"]).json()

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

    second = execute(owner_client, created["id"]).json()
    reread = owner_client.get(f"/api/v1/runs/{first['id']}", headers=ORIGIN).json()

    assert first["model_provider"] == PROVIDER_ID
    assert first["model_name"] == "opaque/anthropic-model"
    assert second["model_provider"] == SECOND_PROVIDER_ID
    assert second["model_name"] == "opaque/openai-model"
    assert reread == first
    assert anthropic.requests[0].model_name == "opaque/anthropic-model"
    assert openai.requests[0].model_name == "opaque/openai-model"
