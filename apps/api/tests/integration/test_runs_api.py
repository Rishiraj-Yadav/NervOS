"""Owner-scoped Run HTTP API tests, including the one-canonical-execution proof."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from nervos_core.application import model_completion
from nervos_core.application.errors import PersistenceUnavailable
from nervos_core.application.model_completion import (
    INTERNAL_EXECUTION_ERROR,
    MODEL_REFUSED,
    ModelProviderError,
    StopOutcome,
    provider_error_message,
)
from nervos_core.application.model_providers import ModelProviderCatalog
from nervos_core.application.trusted_chat import NERVOS_CHAT_SYSTEM_INSTRUCTION
from nervos_core.domain.runs import ModelUsage

INSTANCES = "/api/v1/agent-instances"
ORIGIN = {"Origin": "http://localhost:5173"}
PROVIDER_ID = "anthropic"

FIRST_INPUT = "first independent question"
SECOND_INPUT = "second independent question"

# Derived from the module's own public constants so a newly added normalized failure code is
# covered automatically instead of silently escaping the parametrization.
PROVIDER_ERROR_CODES = sorted(
    value
    for name, value in vars(model_completion).items()
    if name.startswith("MODEL_") or value == INTERNAL_EXECUTION_ERROR
)


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


def make_instance(client: TestClient, **overrides: Any) -> int:
    response = client.post(INSTANCES, json=instance_body(**overrides), headers=ORIGIN)
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def execute(client: TestClient, instance_id: int, text: str = "hello") -> Any:
    return client.post(f"{INSTANCES}/{instance_id}/runs", json={"input": text}, headers=ORIGIN)


def runs_of(client: TestClient, instance_id: int) -> list[dict[str, Any]]:
    response = client.get(f"{INSTANCES}/{instance_id}/runs", headers=ORIGIN)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def test_successful_execution_returns_the_persisted_run(
    owner_client: TestClient, deterministic_completion: Any
) -> None:
    instance_id = make_instance(owner_client)

    response = execute(owner_client, instance_id)

    assert response.status_code == 201, response.text
    body = response.json()
    assert response.headers["Location"] == f"/api/v1/runs/{body['id']}"
    assert body["status"] == "succeeded"
    assert body["output_text"] == "deterministic answer"
    assert body["finish_reason"] == "stop"
    assert body["agent_instance_id"] == instance_id
    assert body["agent_key"] == "nervos.chat"
    assert body["agent_definition_version"] == "1"
    assert body["model_provider"] == PROVIDER_ID
    assert body["model_name"] == "opaque/model"
    assert body["input_text"] == "hello"
    assert body["error_code"] is None and body["error_message"] is None
    assert deterministic_completion.calls == 1

    persisted = owner_client.get(f"/api/v1/runs/{body['id']}", headers=ORIGIN)
    assert persisted.status_code == 200
    assert persisted.json() == body


def test_run_response_never_exposes_limits_or_owner(
    owner_client: TestClient,
) -> None:
    instance_id = make_instance(owner_client)

    body = execute(owner_client, instance_id).json()

    assert "owner_user_id" not in body
    assert "limits" not in body
    for forbidden in ("provider_timeout_ms", "max_output_tokens", "max_model_calls"):
        assert forbidden not in body


def test_usage_is_null_when_nothing_trustworthy_was_reported(
    owner_client: TestClient, deterministic_completion: Any
) -> None:
    instance_id = make_instance(owner_client)
    deterministic_completion.usage = None

    body = execute(owner_client, instance_id).json()

    assert body["usage"] is None


def test_usage_reports_only_trustworthy_counters_without_a_derived_total(
    owner_client: TestClient, deterministic_completion: Any
) -> None:
    instance_id = make_instance(owner_client)
    deterministic_completion.usage = ModelUsage(11, 3, None)

    body = execute(owner_client, instance_id).json()

    assert body["usage"] == {"input_tokens": 11, "output_tokens": 3, "total_tokens": None}


@pytest.mark.parametrize("code", PROVIDER_ERROR_CODES)
def test_normalized_provider_failures_become_persisted_failed_runs(
    owner_client: TestClient, deterministic_completion: Any, code: str
) -> None:
    instance_id = make_instance(owner_client)
    deterministic_completion.error = ModelProviderError(code)

    response = execute(owner_client, instance_id)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "failed"
    assert body["error_code"] == code
    assert body["output_text"] is None
    assert body["finish_reason"] is None
    assert body["error_message"]
    assert len(body["error_message"]) <= 512
    assert deterministic_completion.calls == 1

    persisted = owner_client.get(f"/api/v1/runs/{body['id']}", headers=ORIGIN).json()
    assert persisted == body


def test_refused_and_incomplete_provider_outcomes_fail_safely(
    owner_client: TestClient, deterministic_completion: Any
) -> None:
    instance_id = make_instance(owner_client)
    deterministic_completion.finish_reason = StopOutcome.REFUSED

    body = execute(owner_client, instance_id).json()

    assert body["status"] == "failed"
    assert body["error_code"] == MODEL_REFUSED


@pytest.mark.parametrize(
    "text",
    ["", "   ", "\t\n ", "has\x00nul", "x" * 4001],
)
def test_invalid_input_is_rejected_without_a_run_or_a_model_call(
    owner_client: TestClient, deterministic_completion: Any, text: str
) -> None:
    instance_id = make_instance(owner_client)

    response = execute(owner_client, instance_id, text)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_input"
    assert deterministic_completion.calls == 0
    assert runs_of(owner_client, instance_id) == []


def test_oversized_transport_body_is_rejected_before_execution(
    owner_client: TestClient, deterministic_completion: Any
) -> None:
    instance_id = make_instance(owner_client)

    response = owner_client.post(
        f"{INSTANCES}/{instance_id}/runs",
        content=b'{"input": "' + b"a" * (17 * 1024) + b'"}',
        headers={**ORIGIN, "Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert deterministic_completion.calls == 0
    assert runs_of(owner_client, instance_id) == []


def test_disabled_instance_is_rejected_without_a_run_or_a_model_call(
    owner_client: TestClient, deterministic_completion: Any
) -> None:
    instance_id = make_instance(owner_client)
    owner_client.patch(f"{INSTANCES}/{instance_id}", json={"enabled": False}, headers=ORIGIN)

    response = execute(owner_client, instance_id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "agent_instance_unavailable"
    assert deterministic_completion.calls == 0
    assert runs_of(owner_client, instance_id) == []


def test_unconfigured_provider_is_rejected_without_a_run_or_a_model_call(
    owner_client: TestClient,
    install_provider_catalog: Callable[[ModelProviderCatalog], None],
    unconfigured_provider_catalog: ModelProviderCatalog,
    deterministic_completion: Any,
) -> None:
    instance_id = make_instance(owner_client)
    install_provider_catalog(unconfigured_provider_catalog)

    response = execute(owner_client, instance_id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "model_provider_unavailable"
    assert deterministic_completion.calls == 0
    assert runs_of(owner_client, instance_id) == []


def test_executing_an_unknown_instance_creates_nothing(
    owner_client: TestClient, deterministic_completion: Any
) -> None:
    response = execute(owner_client, 999)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "agent_instance_not_found"
    assert deterministic_completion.calls == 0


def test_run_creation_requires_the_exact_origin(
    owner_client: TestClient, deterministic_completion: Any
) -> None:
    """The route-level origin dependency must hold on its own, not only via the middleware."""
    instance_id = make_instance(owner_client)

    for headers in (None, {"Origin": "http://evil.test"}):
        response = owner_client.post(
            f"{INSTANCES}/{instance_id}/runs",
            json={"input": "hello"},
            headers=headers or {},
        )

        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "invalid_origin"

    assert deterministic_completion.calls == 0
    assert runs_of(owner_client, instance_id) == []


def test_run_creation_rejects_a_non_json_content_type(
    owner_client: TestClient, deterministic_completion: Any
) -> None:
    instance_id = make_instance(owner_client)

    response = owner_client.post(
        f"{INSTANCES}/{instance_id}/runs",
        content="input=hello",
        headers={**ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"
    assert deterministic_completion.calls == 0
    assert runs_of(owner_client, instance_id) == []


@pytest.mark.parametrize("body", [{"owner_user_id": 2}, {"input": "hello", "owner_user_id": 2}])
def test_run_creation_never_accepts_an_owner_from_the_client(
    owner_client: TestClient, deterministic_completion: Any, body: dict[str, Any]
) -> None:
    instance_id = make_instance(owner_client)

    response = owner_client.post(f"{INSTANCES}/{instance_id}/runs", json=body, headers=ORIGIN)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"
    assert deterministic_completion.calls == 0
    assert runs_of(owner_client, instance_id) == []


def test_run_list_and_get_are_owner_scoped(
    owner_client: TestClient,
    seed_user_account: Callable[[str], int],
    sign_in_as: Callable[[str], None],
) -> None:
    instance_id = make_instance(owner_client)
    mine = execute(owner_client, instance_id).json()["id"]
    seed_user_account("intruder")
    sign_in_as("intruder")

    foreign_run = owner_client.get(f"/api/v1/runs/{mine}", headers=ORIGIN)
    missing_run = owner_client.get("/api/v1/runs/9999", headers=ORIGIN)
    foreign_list = owner_client.get(f"{INSTANCES}/{instance_id}/runs", headers=ORIGIN)
    missing_list = owner_client.get(f"{INSTANCES}/9999/runs", headers=ORIGIN)

    assert foreign_run.status_code == missing_run.status_code == 404
    assert foreign_run.json() == missing_run.json()
    assert foreign_run.json()["error"]["code"] == "run_not_found"
    # The nested Run list resolves the parent first, so a foreign instance is not a 200 with an
    # empty history — it is the same 404 a nonexistent instance produces.
    assert foreign_list.status_code == missing_list.status_code == 404
    assert foreign_list.json() == missing_list.json()
    assert foreign_list.json()["error"]["code"] == "agent_instance_not_found"


def test_nested_run_list_requires_an_owned_parent(
    owner_client: TestClient,
    seed_user_account: Callable[[str], int],
    sign_in_as: Callable[[str], None],
) -> None:
    """An owned empty instance is an empty 200; a nonexistent or foreign parent is one 404."""
    empty_instance = make_instance(owner_client, display_name="Empty")
    populated_instance = make_instance(owner_client, display_name="Populated")
    mine = execute(owner_client, populated_instance, FIRST_INPUT).json()["id"]

    # Owned and existing with zero Runs: an empty page, not a not-found.
    empty = owner_client.get(f"{INSTANCES}/{empty_instance}/runs", headers=ORIGIN)
    assert empty.status_code == 200, empty.text
    assert empty.json() == {"items": [], "next_before_id": None}

    # Owned and existing with Runs: the normal newest-first page.
    owned = owner_client.get(f"{INSTANCES}/{populated_instance}/runs", headers=ORIGIN)
    assert owned.status_code == 200, owned.text
    assert [item["id"] for item in owned.json()["items"]] == [mine]

    seed_user_account("intruder")
    sign_in_as("intruder")

    # Nonexistent and foreign are indistinguishable, and neither leaks whether the parent exists.
    foreign = owner_client.get(f"{INSTANCES}/{populated_instance}/runs", headers=ORIGIN)
    missing = owner_client.get(f"{INSTANCES}/9999/runs", headers=ORIGIN)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert foreign.json()["error"]["code"] == "agent_instance_not_found"
    assert FIRST_INPUT not in foreign.text

    sign_in_as("owner")
    history = runs_of(owner_client, populated_instance)
    assert [item["input_text"] for item in history] == [FIRST_INPUT]


def test_executing_a_foreign_instance_creates_no_run_and_calls_no_provider(
    owner_client: TestClient,
    seed_user_account: Callable[[str], int],
    sign_in_as: Callable[[str], None],
    deterministic_completion: Any,
) -> None:
    """Ownership must gate execution itself, not only reads: a foreign instance is simply absent."""
    instance_id = make_instance(owner_client)
    execute(owner_client, instance_id, FIRST_INPUT)
    assert deterministic_completion.calls == 1

    seed_user_account("intruder")
    sign_in_as("intruder")

    foreign = execute(owner_client, instance_id, "stolen question")
    missing = execute(owner_client, 9999, "stolen question")

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert foreign.json()["error"]["code"] == "agent_instance_not_found"
    # No second model call, and nothing was written to the owner's history.
    assert deterministic_completion.calls == 1

    sign_in_as("owner")
    history = runs_of(owner_client, instance_id)
    assert [item["input_text"] for item in history] == [FIRST_INPUT]


def test_every_normalized_provider_code_has_a_safe_message() -> None:
    """Keeps the parametrization above honest as the failure taxonomy grows."""
    assert PROVIDER_ERROR_CODES
    for code in PROVIDER_ERROR_CODES:
        assert provider_error_message(code)


@pytest.mark.parametrize("query", ["limit=0", "limit=51", "before_id=0", "before_id=-3"])
def test_run_list_rejects_pagination_outside_the_contract(
    owner_client: TestClient, query: str
) -> None:
    instance_id = make_instance(owner_client)
    execute(owner_client, instance_id)

    response = owner_client.get(f"{INSTANCES}/{instance_id}/runs?{query}", headers=ORIGIN)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"


def test_run_responses_are_never_cacheable(owner_client: TestClient) -> None:
    instance_id = make_instance(owner_client)
    run_id = execute(owner_client, instance_id).json()["id"]

    responses = (
        owner_client.get(f"{INSTANCES}/{instance_id}/runs", headers=ORIGIN),
        owner_client.get(f"/api/v1/runs/{run_id}", headers=ORIGIN),
    )

    for response in responses:
        assert response.headers["cache-control"] == "no-store", response.request.url


def test_run_list_is_newest_first_and_paginated(owner_client: TestClient) -> None:
    instance_id = make_instance(owner_client)
    first = execute(owner_client, instance_id, "one").json()["id"]
    second = execute(owner_client, instance_id, "two").json()["id"]

    page = runs_of(owner_client, instance_id)
    assert [item["id"] for item in page] == [second, first]

    limited = owner_client.get(f"{INSTANCES}/{instance_id}/runs?limit=1", headers=ORIGIN).json()
    assert [item["id"] for item in limited["items"]] == [second]
    assert limited["next_before_id"] == second

    older = owner_client.get(
        f"{INSTANCES}/{instance_id}/runs?limit=1&before_id={second}", headers=ORIGIN
    ).json()
    assert [item["id"] for item in older["items"]] == [first]


def test_runs_are_immutable_over_http(owner_client: TestClient) -> None:
    instance_id = make_instance(owner_client)
    run_id = execute(owner_client, instance_id).json()["id"]

    assert owner_client.patch(f"/api/v1/runs/{run_id}", json={}, headers=ORIGIN).status_code == 405
    assert owner_client.delete(f"/api/v1/runs/{run_id}", headers=ORIGIN).status_code == 405


def test_terminal_persistence_failure_returns_503_without_leaking_the_answer(
    owner_client: TestClient,
    app_under_test: Any,
    deterministic_completion: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance_id = make_instance(owner_client)
    deterministic_completion.text = "SYNTHETIC-ANSWER-DO-NOT-LEAK"

    def explode(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise PersistenceUnavailable

    monkeypatch.setattr(app_under_test.state.agent_service, "succeed", explode)

    response = execute(owner_client, instance_id)

    captured = capsys.readouterr()
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert "SYNTHETIC-ANSWER-DO-NOT-LEAK" not in response.text
    assert "SYNTHETIC-ANSWER-DO-NOT-LEAK" not in captured.out
    assert "SYNTHETIC-ANSWER-DO-NOT-LEAK" not in captured.err
    # The Run was created and started but never reached a terminal state.
    assert [item["status"] for item in runs_of(owner_client, instance_id)] == ["running"]


def test_a_second_run_sends_only_the_fixed_instruction_and_that_input(
    owner_client: TestClient, deterministic_completion: Any
) -> None:
    """The B3/F boundary: prior Runs are never supplied as model context."""
    instance_id = make_instance(owner_client)
    deterministic_completion.text = "FIRST-ANSWER-MARKER"
    execute(owner_client, instance_id, FIRST_INPUT)

    deterministic_completion.text = "SECOND-ANSWER-MARKER"
    execute(owner_client, instance_id, SECOND_INPUT)

    assert deterministic_completion.calls == 2
    second = deterministic_completion.requests[1]
    assert second.system_instruction == NERVOS_CHAT_SYSTEM_INSTRUCTION
    assert second.user_text == SECOND_INPUT
    assert FIRST_INPUT not in second.user_text
    assert "FIRST-ANSWER-MARKER" not in second.user_text
    assert "SECOND-ANSWER-MARKER" not in second.user_text
    assert second.model_name == "opaque/model"
    assert second.max_output_tokens == 1024
    assert second.timeout_ms == 60000

    history = runs_of(owner_client, instance_id)
    assert [item["input_text"] for item in history] == [SECOND_INPUT, FIRST_INPUT]


def test_editing_an_instance_does_not_alter_existing_runs(owner_client: TestClient) -> None:
    instance_id = make_instance(owner_client)
    run = execute(owner_client, instance_id).json()

    owner_client.patch(
        f"{INSTANCES}/{instance_id}",
        json={
            "display_name": "Renamed",
            "model_provider": PROVIDER_ID,
            "model_name": "opaque/changed-model",
        },
        headers=ORIGIN,
    )

    after = owner_client.get(f"/api/v1/runs/{run['id']}", headers=ORIGIN).json()
    assert after == run
    assert after["model_name"] == "opaque/model"


def test_disabling_an_instance_does_not_cancel_existing_runs(owner_client: TestClient) -> None:
    instance_id = make_instance(owner_client)
    run = execute(owner_client, instance_id).json()

    owner_client.patch(f"{INSTANCES}/{instance_id}", json={"enabled": False}, headers=ORIGIN)

    after = owner_client.get(f"/api/v1/runs/{run['id']}", headers=ORIGIN).json()
    assert after["status"] == "succeeded"
    assert runs_of(owner_client, instance_id) == [after]
