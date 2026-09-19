"""C7 Run Events API: the owner-scoped timeline, its projection, and its pagination.

The Event endpoint is the Run's own sub-resource, so it inherits the Run's privacy contract exactly:
a foreign id and a nonexistent one are the same 404 with the same body, and the timeline can never
be used to probe whether another user's Run exists. What it publishes is an explicit allow-list of
safe durable facts, asserted here as an exact key set so a future column cannot leak by default.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nervos_core.application.job_execution import LEASE_DURATION, ClaimedAttempt
from nervos_core.application.model_completion import MODEL_RATE_LIMITED, safe_error_message
from nervos_core.application.retry_policy import PRODUCTION_RETRY_POLICY
from nervos_core.domain.jobs import RetryDisposition
from nervos_core.domain.runs import ModelUsage
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobExecutionPersistence,
)
from sqlalchemy import Engine, text
from sqlalchemy import event as sqlalchemy_event

INSTANCES = "/api/v1/agent-instances"
ORIGIN = {"Origin": "http://localhost:5173"}
PROVIDER_ID = "anthropic"
ANTHROPIC = ("anthropic",)
WIDE = 64


def claimable_now() -> datetime:
    """An instant safely after a Run accepted moments ago became claimable.

    The API stamps a Job's `available_at` from the real clock at submission, so an execution-plane
    clock pinned to a fixed calendar date would be *before* it and the Job would never be claimable.
    """
    return datetime.now(UTC) + timedelta(seconds=1)


# The whole public projection. Anything else in a response is a leak by definition.
EVENT_FIELDS = {
    "sequence",
    "event_type",
    "created_at",
    "attempt_number",
    "code",
    "message",
    "available_at",
    "tool_invocation_id",
}


def make_instance(client: TestClient, **overrides: Any) -> int:
    body: dict[str, Any] = {
        "agent_key": "nervos.chat",
        "agent_definition_version": "1",
        "display_name": "Chat",
        "model_provider": PROVIDER_ID,
        "model_name": "opaque/model",
    }
    body.update(overrides)
    response = client.post(INSTANCES, json=body, headers=ORIGIN)
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def submit(client: TestClient, instance_id: int, text_value: str = "hello") -> int:
    response = client.post(
        f"{INSTANCES}/{instance_id}/runs", json={"input": text_value}, headers=ORIGIN
    )
    assert response.status_code == 202, response.text
    return int(response.json()["id"])


def events_of(client: TestClient, run_id: int, query: str = "") -> Any:
    return client.get(f"/api/v1/runs/{run_id}/events{query}", headers=ORIGIN)


def execution(engine: Engine) -> SqlAlchemyJobExecutionPersistence:
    return SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None)


def drive_retrying_run(app: FastAPI, run_id: int) -> None:
    """Take one accepted Run through a real rate limit, retry, and success."""
    engine = app.state.database_engine
    now = claimable_now()

    def claim(at: datetime) -> ClaimedAttempt:
        claimed = execution(engine).claim_next(
            worker_id="worker-1",
            provider_ids=ANTHROPIC,
            max_active=WIDE,
            now=at,
            lease_duration=LEASE_DURATION,
        )
        assert claimed is not None
        return claimed

    first = claim(now)
    assert execution(engine).start_attempt(first, now=now) is True
    execution(engine).record_failure(
        first,
        error_code=MODEL_RATE_LIMITED,
        error_message=safe_error_message(MODEL_RATE_LIMITED),
        retry_disposition=RetryDisposition.SAFE_TO_RETRY,
        usage=ModelUsage(1, 1, 2),
        elapsed_ms=5,
        anchor_at=now,
        retry_policy=PRODUCTION_RETRY_POLICY,
        now=now,
    )
    # Past the retry backoff, so the Job is due rather than merely scheduled.
    now = now + timedelta(seconds=10)
    second = claim(now)
    assert execution(engine).start_attempt(second, now=now) is True
    assert execution(engine).succeed(
        second,
        output_text="done",
        finish_reason="stop",
        usage=ModelUsage(1, 1, 2),
        elapsed_ms=5,
        now=now,
    )
    assert second.run_id == run_id


# ---------------------------------------------------------------------------------------
# The owner's read
# ---------------------------------------------------------------------------------------


def test_the_owner_reads_a_complete_ordered_timeline(owner_client: TestClient) -> None:
    instance_id = make_instance(owner_client)
    run_id = submit(owner_client, instance_id)

    body = events_of(owner_client, run_id).json()

    assert [item["sequence"] for item in body["items"]] == [1, 2]
    assert [item["event_type"] for item in body["items"]] == ["run.created", "run.queued"]
    assert body["next_after_sequence"] is None
    assert body["items"][0]["created_at"] is not None


def test_the_timeline_reports_a_retry_with_its_due_instant(
    owner_client: TestClient, app_under_test: FastAPI
) -> None:
    instance_id = make_instance(owner_client)
    run_id = submit(owner_client, instance_id)
    drive_retrying_run(app_under_test, run_id)

    items = events_of(owner_client, run_id).json()["items"]
    types = [item["event_type"] for item in items]

    assert types.index("retry.scheduled") > types.index("attempt.failed")
    assert types.count("attempt.claimed") == 2
    assert types[-1] == "run.succeeded"
    scheduled = items[types.index("retry.scheduled")]
    assert scheduled["available_at"] is not None
    failed = items[types.index("attempt.failed")]
    assert failed["code"] == MODEL_RATE_LIMITED
    assert failed["message"] == safe_error_message(MODEL_RATE_LIMITED)
    assert failed["attempt_number"] == 1
    # The durable sequence is contiguous, which is what makes the timeline provably complete.
    assert [item["sequence"] for item in items] == list(range(1, len(items) + 1))


# ---------------------------------------------------------------------------------------
# The projection
# ---------------------------------------------------------------------------------------


def test_every_event_carries_exactly_the_public_allow_list(
    owner_client: TestClient, app_under_test: FastAPI
) -> None:
    instance_id = make_instance(owner_client)
    run_id = submit(owner_client, instance_id)
    drive_retrying_run(app_under_test, run_id)

    items = events_of(owner_client, run_id).json()["items"]

    assert items
    for item in items:
        assert set(item) == EVENT_FIELDS, sorted(set(item) ^ EVENT_FIELDS)


def test_a_non_tool_event_publishes_a_null_tool_invocation_id(
    owner_client: TestClient,
    app_under_test: FastAPI,
    seed_user_account: Callable[[str], int],
    sign_in_as: Callable[[str], None],
) -> None:
    """The additive handle is explicit null for Stage C events, not an absent key.

    A tool-free Run predates any invocation, so every event carries `tool_invocation_id` as null
    rather than omitting it: a client can bind the field unconditionally. The endpoint, its keyset
    pagination, and the foreign-versus-missing 404 are otherwise untouched by the new column.
    """
    instance_id = make_instance(owner_client)
    run_id = submit(owner_client, instance_id)
    drive_retrying_run(app_under_test, run_id)

    response = events_of(owner_client, run_id, "?limit=3")
    assert response.status_code == 200
    body = response.json()
    assert body["items"]
    assert all(item["tool_invocation_id"] is None for item in body["items"])
    assert all("tool_invocation_id" in item for item in body["items"])
    # The keyset cursor still advances across the untouched Stage C history.
    assert body["next_after_sequence"] == 3

    seed_user_account("intruder")
    sign_in_as("intruder")
    foreign = events_of(owner_client, run_id)
    missing = events_of(owner_client, 9999)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()


def test_the_projection_never_names_an_internal_identifier(
    owner_client: TestClient, app_under_test: FastAPI
) -> None:
    """An execution-plane identifier reaching this response would be a contract break."""
    instance_id = make_instance(owner_client)
    run_id = submit(owner_client, instance_id)
    drive_retrying_run(app_under_test, run_id)

    response = events_of(owner_client, run_id)
    body = response.text

    for forbidden in (
        "claim_token",
        "worker_id",
        "lease",
        "heartbeat",
        "job_id",
        "attempt_id",
        "queue_partition",
        "last_served_attempt_id",
        "active_count",
        "pending_count",
        "disposition",
        "max_attempts",
        "attempt_count",
    ):
        assert forbidden not in body, forbidden
    # The Run identifier is implicit in the path and is not repeated in the payload.
    assert '"run_id"' not in body


# ---------------------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------------------


def test_the_cursor_walks_the_history_without_missing_or_duplicating(
    owner_client: TestClient, app_under_test: FastAPI
) -> None:
    instance_id = make_instance(owner_client)
    run_id = submit(owner_client, instance_id)
    drive_retrying_run(app_under_test, run_id)

    collected: list[int] = []
    cursor = 0
    for _ in range(50):
        body = events_of(owner_client, run_id, f"?limit=5&after_sequence={cursor}").json()
        collected.extend(item["sequence"] for item in body["items"])
        if body["next_after_sequence"] is None:
            break
        cursor = body["next_after_sequence"]
    else:  # pragma: no cover - the cursor must always converge
        raise AssertionError("the cursor never reached the end of the history")

    assert collected == list(range(1, 10))
    assert len(collected) == len(set(collected))


def test_a_full_final_page_ends_with_a_null_cursor_after_one_empty_probe(
    owner_client: TestClient, app_under_test: FastAPI
) -> None:
    """A page that happens to end exactly on the boundary costs one empty request, not a count."""
    instance_id = make_instance(owner_client)
    run_id = submit(owner_client, instance_id)
    drive_retrying_run(app_under_test, run_id)

    full = events_of(owner_client, run_id, "?limit=9").json()
    assert len(full["items"]) == 9
    # Every page that came back full yields a cursor, including the last one.
    assert full["next_after_sequence"] == 9

    probe = events_of(owner_client, run_id, "?limit=9&after_sequence=9").json()
    assert probe["items"] == []
    assert probe["next_after_sequence"] is None


def test_a_cursor_past_the_end_is_an_empty_page_not_an_error(
    owner_client: TestClient,
) -> None:
    instance_id = make_instance(owner_client)
    run_id = submit(owner_client, instance_id)

    body = events_of(owner_client, run_id, "?after_sequence=9999").json()

    assert body == {"items": [], "next_after_sequence": None}


@pytest.mark.parametrize(
    "query",
    ["limit=0", "limit=201", "limit=-1", "after_sequence=-1", "limit=abc", "after_sequence=x"],
)
def test_an_invalid_query_is_rejected_by_the_framework(
    owner_client: TestClient, query: str
) -> None:
    instance_id = make_instance(owner_client)
    run_id = submit(owner_client, instance_id)

    response = events_of(owner_client, run_id, f"?{query}")

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"


def test_the_page_defaults_and_bounds_are_the_published_contract(
    owner_client: TestClient, app_under_test: FastAPI
) -> None:
    instance_id = make_instance(owner_client)
    run_id = submit(owner_client, instance_id)
    drive_retrying_run(app_under_test, run_id)

    assert len(events_of(owner_client, run_id).json()["items"]) == 9
    assert len(events_of(owner_client, run_id, "?limit=1").json()["items"]) == 1
    assert len(events_of(owner_client, run_id, "?limit=200").json()["items"]) == 9


# ---------------------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------------------


def test_a_foreign_runs_events_are_indistinguishable_from_a_missing_runs(
    owner_client: TestClient,
    seed_user_account: Callable[[str], int],
    sign_in_as: Callable[[str], None],
) -> None:
    instance_id = make_instance(owner_client)
    mine = submit(owner_client, instance_id)
    seed_user_account("intruder")
    sign_in_as("intruder")

    foreign = events_of(owner_client, mine)
    missing = events_of(owner_client, 9999)

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert foreign.json()["error"]["code"] == "run_not_found"


def test_reading_a_timeline_requires_authentication(client: TestClient) -> None:
    response = client.get("/api/v1/runs/1/events", headers=ORIGIN)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


def test_the_timeline_is_read_only(client: TestClient, owner_client: TestClient) -> None:
    """No mutation verb exists on this sub-resource, so a retry cannot be driven from it."""
    instance_id = make_instance(owner_client)
    run_id = submit(owner_client, instance_id)

    for method in ("post", "patch", "put", "delete"):
        response = getattr(client, method)(f"/api/v1/runs/{run_id}/events", headers=ORIGIN)
        assert response.status_code == 405, (method, response.status_code)


def test_the_timeline_never_exposes_a_scheduling_or_fairness_concept(
    owner_client: TestClient, app_under_test: FastAPI
) -> None:
    """C6's scheduling internals stay private: the timeline reports one Run's own facts."""
    instance_id = make_instance(owner_client)
    run_id = submit(owner_client, instance_id)
    drive_retrying_run(app_under_test, run_id)

    body = events_of(owner_client, run_id).text.lower()

    for forbidden in ("queue_position", "fair_share", "leader_election", "partition", "position"):
        assert forbidden not in body, forbidden


# ---------------------------------------------------------------------------------------
# The derived Run summary
# ---------------------------------------------------------------------------------------


def test_the_derived_phase_tracks_the_durable_job_through_its_life(
    owner_client: TestClient, app_under_test: FastAPI
) -> None:
    instance_id = make_instance(owner_client)
    run_id = submit(owner_client, instance_id)

    def phase() -> tuple[str | None, str | None]:
        body = owner_client.get(f"/api/v1/runs/{run_id}", headers=ORIGIN).json()
        return body["execution_phase"], body["retry_available_at"]

    assert phase() == ("queued", None)

    engine = app_under_test.state.database_engine
    now = claimable_now()
    claimed = execution(engine).claim_next(
        worker_id="worker-1",
        provider_ids=ANTHROPIC,
        max_active=WIDE,
        now=now,
        lease_duration=LEASE_DURATION,
    )
    assert claimed is not None
    assert phase() == ("claimed", None)

    assert execution(engine).start_attempt(claimed, now=now) is True
    assert phase() == ("running", None)

    execution(engine).record_failure(
        claimed,
        error_code=MODEL_RATE_LIMITED,
        error_message=safe_error_message(MODEL_RATE_LIMITED),
        retry_disposition=RetryDisposition.SAFE_TO_RETRY,
        usage=ModelUsage(1, 1, 2),
        elapsed_ms=5,
        anchor_at=now,
        retry_policy=PRODUCTION_RETRY_POLICY,
        now=now,
    )
    # Waiting to retry is still a `running` Run; only the derived phase distinguishes it.
    phase_name, retry_at = phase()
    assert phase_name == "retry_wait"
    assert retry_at is not None
    assert owner_client.get(f"/api/v1/runs/{run_id}", headers=ORIGIN).json()["status"] == "running"


@pytest.mark.parametrize(
    ("expected", "code", "message"),
    [
        ("succeeded", None, None),
        ("failed", "internal_execution_error", "The model execution failed safely."),
        ("cancelled", "execution_cancelled", "This run was cancelled by its owner."),
    ],
)
def test_the_phase_is_the_jobs_own_terminal_status_verbatim(
    owner_client: TestClient,
    app_under_test: FastAPI,
    expected: str,
    code: str | None,
    message: str | None,
) -> None:
    """No second vocabulary is invented: the phase is the durable Job status, unmapped."""
    instance_id = make_instance(owner_client)
    run_id = submit(owner_client, instance_id)
    engine = app_under_test.state.database_engine
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE jobs SET status=:s, finished_at=:n, error_code=:c, error_message=:m,"
                " cancel_requested_at=:cancel WHERE run_id=:r"
            ),
            {
                "s": expected,
                "n": now,
                "c": code,
                "m": message,
                # A cancelled Job must record when cancellation was requested.
                "cancel": now if expected == "cancelled" else None,
                "r": run_id,
            },
        )

    body = owner_client.get(f"/api/v1/runs/{run_id}", headers=ORIGIN).json()

    assert body["execution_phase"] == expected
    assert body["retry_available_at"] is None


def test_the_timeline_and_the_run_summary_agree(
    owner_client: TestClient, app_under_test: FastAPI
) -> None:
    """The terminal Event and the terminal phase come from the same committed state."""
    instance_id = make_instance(owner_client)
    run_id = submit(owner_client, instance_id)
    drive_retrying_run(app_under_test, run_id)

    summary = owner_client.get(f"/api/v1/runs/{run_id}", headers=ORIGIN).json()
    items = events_of(owner_client, run_id).json()["items"]

    assert summary["status"] == "succeeded"
    assert summary["execution_phase"] == "succeeded"
    assert items[-1]["event_type"] == "run.succeeded"


def test_no_run_read_pulls_a_second_job_query_per_run(
    owner_client: TestClient, app_under_test: FastAPI
) -> None:
    """The phase is part of the same committed read, so a page costs no extra round trips."""
    instance_id = make_instance(owner_client)
    for index in range(3):
        submit(owner_client, instance_id, f"run-{index}")

    statements: list[str] = []
    engine = app_under_test.state.database_engine

    def record(*args: Any) -> None:
        statements.append(str(args[2]))

    sqlalchemy_event.listen(engine, "before_cursor_execute", record)
    try:
        response = owner_client.get(f"{INSTANCES}/{instance_id}/runs", headers=ORIGIN)
    finally:
        sqlalchemy_event.remove(engine, "before_cursor_execute", record)

    assert response.status_code == 200
    selects = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT") and "runs" in statement
    ]
    assert len(selects) == 1, selects


def test_the_events_route_does_not_disturb_the_existing_run_surface(
    owner_client: TestClient,
) -> None:
    """C7 adds one route; every existing Run response keeps its shape."""
    instance_id = make_instance(owner_client)
    run_id = submit(owner_client, instance_id)

    single = owner_client.get(f"/api/v1/runs/{run_id}", headers=ORIGIN)
    listed = owner_client.get(f"{INSTANCES}/{instance_id}/runs", headers=ORIGIN)

    assert single.status_code == 200
    assert listed.status_code == 200
    assert single.json() == listed.json()["items"][0]
    assert listed.json()["next_before_id"] is None


def test_the_timeline_is_scoped_to_its_own_run(
    owner_client: TestClient, app_under_test: FastAPI
) -> None:
    instance_id = make_instance(owner_client)
    first = submit(owner_client, instance_id, "first")
    drive_retrying_run(app_under_test, first)
    second = submit(owner_client, instance_id, "second")

    first_events = events_of(owner_client, first).json()["items"]
    second_events = events_of(owner_client, second).json()["items"]

    assert len(first_events) == 9
    assert [item["event_type"] for item in second_events] == ["run.created", "run.queued"]
    # Each Run numbers its own Events from 1: neither timeline leaks the other's history.
    assert [item["sequence"] for item in second_events] == [1, 2]


def test_the_default_database_is_never_touched(owner_client: TestClient) -> None:
    """The C7 read path reads only the migrated temporary database the suite configured."""
    default = (Path.home() / ".nervos" / "nervos.db").resolve(strict=False)
    before = default.stat().st_size if default.exists() else None

    instance_id = make_instance(owner_client)
    run_id = submit(owner_client, instance_id)
    assert events_of(owner_client, run_id).status_code == 200

    after = default.stat().st_size if default.exists() else None
    assert after == before


def test_the_api_process_still_composes_no_execution_capability(
    owner_client: TestClient, app_under_test: FastAPI
) -> None:
    """Observability did not widen the control plane: it can read a timeline and nothing else."""
    engine = app_under_test.state.database_engine
    instance_id = make_instance(owner_client)
    run_id = submit(owner_client, instance_id)

    assert events_of(owner_client, run_id).status_code == 200
    with engine.connect() as connection:
        # Reading a timeline claims nothing, starts nothing, and creates no Attempt.
        assert connection.scalar(text("SELECT count(*) FROM job_attempts")) == 0
