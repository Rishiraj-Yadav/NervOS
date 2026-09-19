"""D7 API-contract acceptance: the owner scope, and the shape of the observability surface.

The control plane is the only place a user can read execution facts, so its two D7 obligations are
adversarial rather than cosmetic: a foreign owner must learn nothing, and the one surface D7 added
must not have widened. Both journeys here run against the composed application -- route table
included -- rather than against a source file, which is what makes them a contract check rather than
a second copy of an architecture guard.

The source-level facts these journeys complement are already pinned by
`packages/nervos-core/tests/architecture/test_boundaries.py::test_c7_adds_only_the_reviewed_observability_surface`
(one GET, an allow-listed projection, no write primitive). Where that guard already proves a fact,
this suite references it instead of restating it, and asserts the *composed* consequence: the
route table the application actually serves.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from nervos_core.application.mcp_connections import ConnectionTransport
from nervos_core.infrastructure.database.mcp_connections import (
    SqlAlchemyMcpConnectionPersistence,
)

INSTANCES = "/api/v1/agent-instances"
RUNS = "/api/v1/runs"
MCP = "/api/v1/mcp-connections"
ORIGIN = {"Origin": "http://localhost:5173"}
PROVIDER_ID = "anthropic"

# A foreign id that cannot exist, so "missing" and "foreign" are compared under one response shape.
ABSENT_ID = 999_999


def instance_body() -> dict[str, Any]:
    return {
        "agent_key": "nervos.chat",
        "agent_definition_version": "1",
        "display_name": "Chat",
        "model_provider": PROVIDER_ID,
        "model_name": "opaque/model",
    }


def make_instance(client: TestClient) -> int:
    response = client.post(INSTANCES, json=instance_body(), headers=ORIGIN)
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def submit(client: TestClient, instance_id: int, text_value: str = "hello") -> int:
    response = client.post(
        f"{INSTANCES}/{instance_id}/runs", json={"input": text_value}, headers=ORIGIN
    )
    assert response.status_code == 202, response.text
    return int(response.json()["id"])


def _owned_connection(app: FastAPI) -> int:
    """Record one MCP connection for the first administrator through the shipped persistence.

    The public create route deliberately validates the target against operator-declared origins,
    and a test host declares none -- so it would refuse every literal endpoint before ownership is
    ever in question. This journey's subject is cross-owner enforcement, not create validation, so
    the row is written through the same persistence the route uses and then read back *through the
    route*.
    """
    return SqlAlchemyMcpConnectionPersistence(app.state.database_engine).create(
        owner_user_id=1,
        display_name="owned tools",
        transport=ConnectionTransport.HTTP,
        endpoint="https://tools.example/mcp",
        server_key=None,
        credential_ref=None,
        now=datetime.now(UTC),
    )


def _assert_indistinguishable(foreign: Any, absent: Any, code: str) -> None:
    """A foreign id and a nonexistent one must share status *and* body, and never be a 403.

    A 403 is exactly the leak this guards: it would confirm the resource exists while denying it.
    """
    assert foreign.status_code == absent.status_code == 404, (foreign.text, absent.text)
    assert foreign.status_code != 403
    assert foreign.json() == absent.json()
    assert foreign.json()["error"]["code"] == code


def test_a_foreign_owner_cannot_read_events_or_manage_a_run_and_an_mcp_connection(
    owner_client: TestClient,
    app_under_test: FastAPI,
    seed_user_account: Callable[[str], int],
    sign_in_as: Callable[[str], None],
) -> None:
    """One integrated journey: user A's Agent Instance, connection and Run are all opaque to B."""
    instance_id = make_instance(owner_client)
    run_id = submit(owner_client, instance_id)
    connection_id = _owned_connection(app_under_test)

    # A owns all three and can reach them.
    assert owner_client.get(f"{RUNS}/{run_id}", headers=ORIGIN).status_code == 200
    assert owner_client.get(f"{RUNS}/{run_id}/events", headers=ORIGIN).status_code == 200
    assert owner_client.get(f"{MCP}/{connection_id}", headers=ORIGIN).status_code == 200

    seed_user_account("intruder")
    sign_in_as("intruder")

    _assert_indistinguishable(
        owner_client.get(f"{RUNS}/{run_id}", headers=ORIGIN),
        owner_client.get(f"{RUNS}/{ABSENT_ID}", headers=ORIGIN),
        "run_not_found",
    )
    _assert_indistinguishable(
        owner_client.get(f"{RUNS}/{run_id}/events", headers=ORIGIN),
        owner_client.get(f"{RUNS}/{ABSENT_ID}/events", headers=ORIGIN),
        "run_not_found",
    )
    # Cancellation is a management action on A's Run and must be as invisible as reading it.
    _assert_indistinguishable(
        owner_client.post(f"{RUNS}/{run_id}/cancel", headers=ORIGIN),
        owner_client.post(f"{RUNS}/{ABSENT_ID}/cancel", headers=ORIGIN),
        "run_not_found",
    )

    # Every management verb on A's connection resolves to the same not-found as a missing id.
    operations: list[tuple[str, str, dict[str, Any] | None]] = [
        ("get", f"{MCP}/{connection_id}", None),
        ("post", f"{MCP}/{connection_id}/refresh", None),
        ("post", f"{MCP}/{connection_id}/enable", None),
        ("post", f"{MCP}/{connection_id}/disable", None),
        ("patch", f"{MCP}/{connection_id}", {"display_name": "hijacked"}),
        ("delete", f"{MCP}/{connection_id}", None),
    ]
    for method, path, body in operations:
        absent_path = f"{MCP}/{ABSENT_ID}" + path.rsplit(str(connection_id), 1)[-1]
        _assert_indistinguishable(
            owner_client.request(method, path, headers=ORIGIN, json=body),
            owner_client.request(method, absent_path, headers=ORIGIN, json=body),
            "mcp_connection_not_found",
        )

    # B's rename attempt changed nothing: A still reads the original display name.
    sign_in_as("owner")
    assert owner_client.get(f"{MCP}/{connection_id}", headers=ORIGIN).json()["display_name"] == (
        "owned tools"
    )


def _allows_null(schema: dict[str, Any]) -> bool:
    if schema.get("type") == "null":
        return True
    return any(option.get("type") == "null" for option in schema.get("anyOf", []))


def test_the_observability_surface_is_one_owner_scoped_get_with_an_additive_handle(
    owner_client: TestClient, app_under_test: FastAPI
) -> None:
    """The composed route table proves D7 added no second surface and widened nothing.

    `test_c7_adds_only_the_reviewed_observability_surface` already pins the source (one GET route,
    no mutation verb, an allow-listed projection with no write primitive). This asserts the same
    facts against the OpenAPI document the application actually serves, so a route injected by
    composition cannot slip past a source-text guard, and it shows `tool_invocation_id` is
    additive and nullable rather than a required widening of the response.
    """
    openapi = app_under_test.openapi()
    paths: dict[str, Any] = openapi["paths"]

    event_paths = sorted(path for path in paths if path.endswith("/runs/{run_id}/events"))
    assert event_paths == ["/api/v1/runs/{run_id}/events"]
    # Exactly one verb, and it is a read: no POST/PATCH/PUT/DELETE exists for Run Events.
    assert set(paths[event_paths[0]]) == {"get"}

    # There is no public ToolInvocation route at all: the invocation is a handle on a timeline row,
    # never a resource of its own.
    assert [path for path in paths if "invocation" in path.lower()] == []

    # The handle is additive (not required) and nullable, so an Event with no tool work serialises
    # it as null rather than refusing to render.
    projection = openapi["components"]["schemas"]["RunEventResponse"]
    assert "tool_invocation_id" in projection["properties"]
    assert _allows_null(projection["properties"]["tool_invocation_id"])
    assert "tool_invocation_id" not in projection.get("required", [])

    # Behaviorally: every Event of a Run composed by the control plane carries the key, null,
    # because the API composes no executor and can therefore never produce tool work itself.
    instance_id = make_instance(owner_client)
    run_id = submit(owner_client, instance_id)
    items = owner_client.get(f"{RUNS}/{run_id}/events", headers=ORIGIN).json()["items"]
    assert items
    for item in items:
        assert "tool_invocation_id" in item
        assert item["tool_invocation_id"] is None
