"""Owner-scoped Run HTTP API tests for the C2 durable-acceptance contract.

The public POST no longer executes anything: it durably accepts work and returns 202. These
tests prove that contract from the outside - status, body, Location, the durable rows the
acceptance commits, admission limits, and the unchanged security surface - while execution
itself is proven in the Worker suite, where a Worker actually runs a claimed Job.
"""

from __future__ import annotations

from collections.abc import Callable
from threading import Barrier, Thread
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nervos_core.application.model_providers import ModelProviderCatalog
from sqlalchemy import text

INSTANCES = "/api/v1/agent-instances"
ORIGIN = {"Origin": "http://localhost:5173"}
PROVIDER_ID = "anthropic"
SECOND_PROVIDER_ID = "openai"

FIRST_INPUT = "first independent question"
SECOND_INPUT = "second independent question"


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


def submit(client: TestClient, instance_id: int, text_value: str = "hello") -> Any:
    return client.post(
        f"{INSTANCES}/{instance_id}/runs", json={"input": text_value}, headers=ORIGIN
    )


def runs_of(client: TestClient, instance_id: int) -> list[dict[str, Any]]:
    response = client.get(f"{INSTANCES}/{instance_id}/runs", headers=ORIGIN)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def row_counts(app: FastAPI) -> dict[str, int]:
    with app.state.database_engine.connect() as connection:
        return {
            table: int(connection.scalar(text(f"SELECT count(*) FROM {table}")) or 0)
            for table in ("runs", "jobs", "job_attempts", "run_events")
        }


def events_of(app: FastAPI, run_id: int) -> list[tuple[int, str]]:
    with app.state.database_engine.connect() as connection:
        return [
            (int(sequence), str(event_type))
            for sequence, event_type in connection.execute(
                text(
                    "SELECT sequence, event_type FROM run_events WHERE run_id=:r ORDER BY sequence"
                ),
                {"r": run_id},
            )
        ]


def test_acceptance_returns_202_with_the_persisted_created_run(
    owner_client: TestClient, app_under_test: FastAPI
) -> None:
    instance_id = make_instance(owner_client)

    response = submit(owner_client, instance_id)

    assert response.status_code == 202, response.text
    body = response.json()
    assert response.headers["Location"] == f"/api/v1/runs/{body['id']}"
    assert body["status"] == "created"
    assert body["agent_instance_id"] == instance_id
    assert body["agent_key"] == "nervos.chat"
    assert body["agent_definition_version"] == "1"
    assert body["model_provider"] == PROVIDER_ID
    assert body["model_name"] == "opaque/model"
    assert body["input_text"] == "hello"
    assert body["output_text"] is None
    assert body["finish_reason"] is None
    assert body["error_code"] is None and body["error_message"] is None
    assert body["usage"] is None
    assert body["elapsed_ms"] is None

    persisted = owner_client.get(f"/api/v1/runs/{body['id']}", headers=ORIGIN)
    assert persisted.status_code == 200
    assert persisted.json() == body
    _ = app_under_test


def test_acceptance_commits_exactly_one_job_and_two_events(
    owner_client: TestClient, app_under_test: FastAPI
) -> None:
    instance_id = make_instance(owner_client)

    run = submit(owner_client, instance_id).json()

    assert row_counts(app_under_test) == {
        "runs": 1,
        "jobs": 1,
        "job_attempts": 0,
        "run_events": 2,
    }
    assert events_of(app_under_test, run["id"]) == [(1, "run.created"), (2, "run.queued")]
    with app_under_test.state.database_engine.connect() as connection:
        job = dict(
            connection.execute(
                text(
                    "SELECT status, attempt_count, max_attempts, claimed_by, model_provider"
                    " FROM jobs"
                )
            )
            .mappings()
            .one()
        )
    assert job == {
        "status": "queued",
        "attempt_count": 0,
        "max_attempts": 3,
        "claimed_by": None,
        "model_provider": PROVIDER_ID,
    }


def test_the_api_process_never_invokes_a_provider(
    owner_client: TestClient, deterministic_completion: Any
) -> None:
    """Acceptance is structural: this process composes no executor and no credential."""
    instance_id = make_instance(owner_client)

    assert submit(owner_client, instance_id).status_code == 202

    assert deterministic_completion.calls == 0
    assert deterministic_completion.requests == []


def test_the_api_holds_no_execution_capability(app_under_test: FastAPI) -> None:
    reachable = set(vars(app_under_test.state))
    assert "run_coordinator" not in reachable
    assert "run_executor" not in reachable
    assert "handler_registry" not in reachable
    assert "completions" not in reachable


def test_acceptance_creates_no_attempt_until_a_worker_claims(
    owner_client: TestClient, app_under_test: FastAPI
) -> None:
    instance_id = make_instance(owner_client)
    for _ in range(3):
        assert submit(owner_client, instance_id).status_code == 202
    assert row_counts(app_under_test) == {
        "runs": 3,
        "jobs": 3,
        "job_attempts": 0,
        "run_events": 6,
    }


def test_invalid_input_is_rejected_without_a_run_job_or_event(
    owner_client: TestClient, app_under_test: FastAPI, deterministic_completion: Any
) -> None:
    instance_id = make_instance(owner_client)

    response = submit(owner_client, instance_id, "\t \n")

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_input"
    assert row_counts(app_under_test)["runs"] == 0
    assert deterministic_completion.calls == 0


def test_a_disabled_instance_is_rejected_without_a_run_job_or_event(
    owner_client: TestClient, app_under_test: FastAPI
) -> None:
    instance_id = make_instance(owner_client)
    disabled = owner_client.patch(
        f"{INSTANCES}/{instance_id}", json={"enabled": False}, headers=ORIGIN
    )
    assert disabled.status_code == 200, disabled.text

    response = submit(owner_client, instance_id)

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "agent_instance_unavailable"
    assert row_counts(app_under_test) == {
        "runs": 0,
        "jobs": 0,
        "job_attempts": 0,
        "run_events": 0,
    }


def test_an_unknown_provider_is_rejected_and_a_known_unconfigured_one_is_accepted(
    owner_client: TestClient,
    install_provider_catalog: Callable[..., None],
) -> None:
    install_provider_catalog(ModelProviderCatalog([], known=[PROVIDER_ID]))
    unknown = owner_client.post(
        INSTANCES,
        json=instance_body(model_provider="not-a-provider"),
        headers=ORIGIN,
    )
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "unknown_model_provider"

    instance_id = make_instance(owner_client)
    accepted = submit(owner_client, instance_id)
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["status"] == "created"


def test_repeated_acceptance_creates_distinct_runs(
    owner_client: TestClient, app_under_test: FastAPI
) -> None:
    instance_id = make_instance(owner_client)

    first = submit(owner_client, instance_id, FIRST_INPUT).json()
    second = submit(owner_client, instance_id, SECOND_INPUT).json()

    assert first["id"] != second["id"]
    assert row_counts(app_under_test)["runs"] == 2
    assert row_counts(app_under_test)["jobs"] == 2


def test_acceptance_succeeds_with_no_worker_running(
    owner_client: TestClient, app_under_test: FastAPI
) -> None:
    """Nothing in this process may claim, so a queued Run simply waits."""
    instance_id = make_instance(owner_client)

    run = submit(owner_client, instance_id).json()

    assert run["status"] == "created"
    assert row_counts(app_under_test) == {
        "runs": 1,
        "jobs": 1,
        "job_attempts": 0,
        "run_events": 2,
    }


def test_capacity_breach_returns_429_without_partial_rows(
    owner_client: TestClient,
    app_under_test: FastAPI,
    install_provider_catalog: Callable[..., None],
) -> None:
    install_provider_catalog(ModelProviderCatalog([], known=[PROVIDER_ID]), max_pending=1)
    instance_id = make_instance(owner_client)
    assert submit(owner_client, instance_id, FIRST_INPUT).status_code == 202
    before = row_counts(app_under_test)

    response = submit(owner_client, instance_id, SECOND_INPUT)

    assert response.status_code == 429, response.text
    assert response.json()["error"]["code"] == "queue_capacity_exceeded"
    assert row_counts(app_under_test) == before
    with app_under_test.state.database_engine.connect() as connection:
        assert connection.scalar(text("SELECT max(id) FROM runs")) == 1


def test_capacity_is_global_across_owners(
    owner_client: TestClient,
    app_under_test: FastAPI,
    seed_user_account: Callable[[str], int],
    sign_in_as: Callable[[str], None],
    install_provider_catalog: Callable[..., None],
) -> None:
    """Pins the accepted C0 global cap: one owner's backlog can refuse another's submission.

    Per-instance and per-owner fairness are deliberately deferred to C6, so this test records
    the limitation rather than leaving it to be discovered later.
    """
    install_provider_catalog(ModelProviderCatalog([], known=[PROVIDER_ID]), max_pending=1)
    mine = make_instance(owner_client)
    assert submit(owner_client, mine).status_code == 202

    seed_user_account("second_owner")
    sign_in_as("second_owner")
    theirs = make_instance(owner_client)

    response = submit(owner_client, theirs)

    assert response.status_code == 429, response.text
    assert response.json()["error"]["code"] == "queue_capacity_exceeded"
    assert row_counts(app_under_test)["runs"] == 1


def test_concurrent_submissions_cannot_exceed_the_capacity(
    owner_client: TestClient,
    install_provider_catalog: Callable[..., None],
) -> None:
    install_provider_catalog(ModelProviderCatalog([], known=[PROVIDER_ID]), max_pending=1)
    instance_id = make_instance(owner_client)
    barrier = Barrier(2)
    statuses: list[int] = []

    def attempt() -> None:
        barrier.wait()
        statuses.append(submit(owner_client, instance_id).status_code)

    threads = [Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(statuses) == [202, 429]


def test_a_persistence_failure_is_never_reported_as_capacity(
    owner_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A busy database must never be reported as a full queue, and vice versa."""
    from nervos_core.application.errors import PersistenceUnavailable
    from nervos_core.infrastructure.database.jobs import SqlAlchemyJobPersistence

    instance_id = make_instance(owner_client)

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise PersistenceUnavailable

    monkeypatch.setattr(SqlAlchemyJobPersistence, "submit", unavailable)

    response = submit(owner_client, instance_id)

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "service_unavailable"
    monkeypatch.undo()


def test_origin_authentication_and_content_type_are_unchanged(
    owner_client: TestClient, deterministic_completion: Any
) -> None:
    instance_id = make_instance(owner_client)

    wrong_type = owner_client.post(
        f"{INSTANCES}/{instance_id}/runs",
        content="input=hello",
        headers={**ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert wrong_type.status_code == 415
    assert wrong_type.json()["error"]["code"] == "unsupported_media_type"

    for headers in (None, {"Origin": "http://evil.test"}):
        response = owner_client.post(
            f"{INSTANCES}/{instance_id}/runs",
            json={"input": "hello"},
            headers=headers or {},
        )
        assert response.status_code == 403, response.text

    assert deterministic_completion.calls == 0
    assert runs_of(owner_client, instance_id) == []


def test_unauthenticated_submission_is_rejected(
    client: TestClient, deterministic_completion: Any
) -> None:
    response = client.post(f"{INSTANCES}/1/runs", json={"input": "hello"}, headers=ORIGIN)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
    assert deterministic_completion.calls == 0


@pytest.mark.parametrize("body", [{"owner_user_id": 2}, {"input": "hello", "owner_user_id": 2}])
def test_submission_never_accepts_an_owner_from_the_client(
    owner_client: TestClient, body: dict[str, Any]
) -> None:
    instance_id = make_instance(owner_client)

    response = owner_client.post(f"{INSTANCES}/{instance_id}/runs", json=body, headers=ORIGIN)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"
    assert runs_of(owner_client, instance_id) == []


def test_run_response_never_exposes_limits_or_owner(owner_client: TestClient) -> None:
    instance_id = make_instance(owner_client)

    body = submit(owner_client, instance_id).json()

    assert "owner_user_id" not in body
    assert "limits" not in body
    for forbidden in ("provider_timeout_ms", "max_output_tokens", "max_model_calls"):
        assert forbidden not in body


def test_submitting_a_foreign_instance_creates_nothing(
    owner_client: TestClient,
    app_under_test: FastAPI,
    seed_user_account: Callable[[str], int],
    sign_in_as: Callable[[str], None],
) -> None:
    instance_id = make_instance(owner_client)
    assert submit(owner_client, instance_id, FIRST_INPUT).status_code == 202

    seed_user_account("intruder")
    sign_in_as("intruder")

    foreign = submit(owner_client, instance_id, "stolen question")
    missing = submit(owner_client, 9999, "stolen question")

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert foreign.json()["error"]["code"] == "agent_instance_not_found"

    sign_in_as("owner")
    assert [item["input_text"] for item in runs_of(owner_client, instance_id)] == [FIRST_INPUT]
    assert row_counts(app_under_test) == {
        "runs": 1,
        "jobs": 1,
        "job_attempts": 0,
        "run_events": 2,
    }


def test_run_list_and_get_are_owner_scoped(
    owner_client: TestClient,
    seed_user_account: Callable[[str], int],
    sign_in_as: Callable[[str], None],
) -> None:
    instance_id = make_instance(owner_client)
    mine = submit(owner_client, instance_id).json()["id"]
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
    mine = submit(owner_client, populated_instance, FIRST_INPUT).json()["id"]

    empty = owner_client.get(f"{INSTANCES}/{empty_instance}/runs", headers=ORIGIN)
    assert empty.status_code == 200, empty.text
    assert empty.json() == {"items": [], "next_before_id": None}

    owned = owner_client.get(f"{INSTANCES}/{populated_instance}/runs", headers=ORIGIN)
    assert owned.status_code == 200, owned.text
    assert [item["id"] for item in owned.json()["items"]] == [mine]

    seed_user_account("intruder")
    sign_in_as("intruder")

    foreign = owner_client.get(f"{INSTANCES}/{populated_instance}/runs", headers=ORIGIN)
    missing = owner_client.get(f"{INSTANCES}/9999/runs", headers=ORIGIN)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert foreign.json()["error"]["code"] == "agent_instance_not_found"
    assert FIRST_INPUT not in foreign.text

    sign_in_as("owner")
    history = runs_of(owner_client, populated_instance)
    assert [item["input_text"] for item in history] == [FIRST_INPUT]


def test_editing_an_instance_after_acceptance_cannot_change_the_queued_job(
    owner_client: TestClient, app_under_test: FastAPI
) -> None:
    instance_id = make_instance(owner_client)
    run = submit(owner_client, instance_id).json()

    patched = owner_client.patch(
        f"{INSTANCES}/{instance_id}",
        json={
            "display_name": "Chat",
            "model_provider": PROVIDER_ID,
            "model_name": "opaque/changed-model",
        },
        headers=ORIGIN,
    )
    assert patched.status_code == 200, patched.text

    after = owner_client.get(f"/api/v1/runs/{run['id']}", headers=ORIGIN).json()
    assert after == run
    assert after["model_name"] == "opaque/model"
    with app_under_test.state.database_engine.connect() as connection:
        assert connection.scalar(text("SELECT model_provider FROM jobs")) == PROVIDER_ID
        assert connection.scalar(text("SELECT model_name FROM runs")) == "opaque/model"


def test_disabling_an_instance_does_not_cancel_an_accepted_run(
    owner_client: TestClient,
) -> None:
    instance_id = make_instance(owner_client)
    run = submit(owner_client, instance_id).json()
    owner_client.patch(f"{INSTANCES}/{instance_id}", json={"enabled": False}, headers=ORIGIN)
    after = owner_client.get(f"/api/v1/runs/{run['id']}", headers=ORIGIN).json()
    assert after["status"] == "created"
    assert runs_of(owner_client, instance_id) == [after]


def test_an_internal_retry_wait_is_projected_as_an_ordinary_running_run(
    owner_client: TestClient, app_under_test: FastAPI
) -> None:
    """C4's retry state is execution-plane detail and must not become a public Run shape.

    A Job waiting to retry has no public representation: the Run it belongs to is simply still
    running, exactly as it was while the provider call was in flight. This asserts the response
    is byte-identical to the same Run before the retry was scheduled, so no Job status, attempt
    number, disposition, deadline, or claim field can leak through the projection.
    """
    instance_id = make_instance(owner_client)
    run = submit(owner_client, instance_id).json()
    created = owner_client.get(f"/api/v1/runs/{run['id']}", headers=ORIGIN).json()

    engine = app_under_test.state.database_engine
    with engine.begin() as connection:
        # Keep every timestamp after the Run's own creation instant: the schema enforces the
        # ordering, and this test is about projection, not about timestamp legality.
        started_at = connection.scalar(
            text("SELECT created_at FROM runs WHERE id=:r"), {"r": run["id"]}
        )
        job_created_at = connection.scalar(
            text("SELECT created_at FROM jobs WHERE run_id=:r"), {"r": run["id"]}
        )
        connection.execute(
            text("UPDATE runs SET status='running', started_at=:now WHERE id=:r"),
            {"now": started_at, "r": run["id"]},
        )
        connection.execute(
            text("UPDATE jobs SET status='retry_wait', available_at=:due WHERE run_id=:r"),
            {"due": job_created_at, "r": run["id"]},
        )

    projected = owner_client.get(f"/api/v1/runs/{run['id']}", headers=ORIGIN).json()
    assert projected["status"] == "running"
    assert projected["error_code"] is None and projected["error_message"] is None
    assert projected["finished_at"] is None and projected["elapsed_ms"] is None
    assert set(projected) == set(created)
    for leak in ("retry_wait", "attempt", "available_at", "claim", "disposition"):
        assert not any(leak in str(key).lower() for key in projected), leak
    assert [item["id"] for item in runs_of(owner_client, instance_id)] == [run["id"]]


@pytest.mark.parametrize("query", ["limit=0", "limit=51", "before_id=0", "before_id=-3"])
def test_invalid_run_list_queries_are_rejected(owner_client: TestClient, query: str) -> None:
    instance_id = make_instance(owner_client)

    response = owner_client.get(f"{INSTANCES}/{instance_id}/runs?{query}", headers=ORIGIN)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"


# -- C5 owner cancellation ---------------------------------------------------------------


def cancel(client: TestClient, run_id: int) -> Any:
    return client.post(f"/api/v1/runs/{run_id}/cancel", headers=ORIGIN)


def test_cancelling_a_queued_run_returns_the_cancelled_run(
    owner_client: TestClient, app_under_test: FastAPI
) -> None:
    instance_id = make_instance(owner_client)
    run_id = int(submit(owner_client, instance_id).json()["id"])

    response = cancel(owner_client, run_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "cancelled"
    # A Run cancelled before execution began invents no start boundary and no duration, and
    # carries no provider error: cancellation is a lifecycle, not a failure.
    assert body["started_at"] is None
    assert body["elapsed_ms"] is None
    assert body["error_code"] is None and body["error_message"] is None
    assert body["output_text"] is None and body["usage"] is None
    assert body["finished_at"] is not None
    # The projection is the Run alone: no Job, Attempt, token, or worker detail leaks.
    for forbidden in ("job", "attempt", "claim_token", "claimed_by", "worker", "disposition"):
        assert forbidden not in body
    # Exactly two events are appended, in order, and no Run-level failure is fabricated.
    assert [event for _, event in events_of(app_under_test, run_id)][-2:] == [
        "cancellation.requested",
        "run.cancelled",
    ]
    assert "run.failed" not in [event for _, event in events_of(app_under_test, run_id)]
    # No Attempt was invented for work that never began.
    assert row_counts(app_under_test)["job_attempts"] == 0


def test_cancelling_twice_is_idempotent(owner_client: TestClient, app_under_test: FastAPI) -> None:
    instance_id = make_instance(owner_client)
    run_id = int(submit(owner_client, instance_id).json()["id"])
    first = cancel(owner_client, run_id)
    events_after_first = events_of(app_under_test, run_id)

    second = cancel(owner_client, run_id)

    assert second.status_code == 200
    assert second.json() == first.json()
    assert events_of(app_under_test, run_id) == events_after_first


def test_a_cancelled_run_projects_as_cancelled_in_reads(
    owner_client: TestClient,
) -> None:
    instance_id = make_instance(owner_client)
    run_id = int(submit(owner_client, instance_id).json()["id"])
    cancel(owner_client, run_id)

    single = owner_client.get(f"/api/v1/runs/{run_id}", headers=ORIGIN)
    assert single.status_code == 200
    assert single.json()["status"] == "cancelled"
    listed = [run for run in runs_of(owner_client, instance_id) if run["id"] == run_id]
    assert listed and listed[0]["status"] == "cancelled"


def test_cancelling_a_foreign_run_is_indistinguishable_from_a_missing_one(
    owner_client: TestClient,
    seed_user_account: Callable[[str], int],
    sign_in_as: Callable[[str], None],
) -> None:
    instance_id = make_instance(owner_client)
    run_id = int(submit(owner_client, instance_id).json()["id"])
    seed_user_account("intruder")
    sign_in_as("intruder")

    foreign = cancel(owner_client, run_id)
    missing = cancel(owner_client, run_id + 9999)

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert foreign.json()["error"]["code"] == "run_not_found"


def test_cancellation_requires_the_configured_origin(owner_client: TestClient) -> None:
    instance_id = make_instance(owner_client)
    run_id = int(submit(owner_client, instance_id).json()["id"])

    assert owner_client.post(f"/api/v1/runs/{run_id}/cancel").status_code == 403


def test_cancelling_an_already_terminal_run_is_a_conflict(
    owner_client: TestClient, app_under_test: FastAPI
) -> None:
    """A succeeded Run is history; cancellation reports that truthfully instead of rewriting it."""
    instance_id = make_instance(owner_client)
    run_id = int(submit(owner_client, instance_id).json()["id"])
    with app_under_test.state.database_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE runs SET status='succeeded', started_at=created_at,"
                " finished_at=created_at, output_text='done', finish_reason='stop',"
                " elapsed_ms=1 WHERE id=:r"
            ),
            {"r": run_id},
        )

    response = cancel(owner_client, run_id)

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "run_not_cancellable"
    assert (
        owner_client.get(f"/api/v1/runs/{run_id}", headers=ORIGIN).json()["status"] == "succeeded"
    )
