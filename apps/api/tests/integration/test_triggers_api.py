"""Stage E4 trigger management HTTP surface: routes, authority, secrets, and provenance.

Every request goes through the composed ASGI app, so the assertions cover the transport, the
middleware ordering and the application services together. The E2/E3 regressions at the end drive
the *existing* scheduler and ingress against triggers created through the new management API,
which is what proves the management surface did not grow a second execution path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

ORIGIN: dict[str, str] = {"Origin": "http://localhost:5173"}
PROVIDER_ID: str = "anthropic"

TRIGGERS = "/api/v1/triggers"
INSTANCES = "/api/v1/agent-instances"
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def make_instance(client: TestClient, name: str = "Chat") -> int:
    response = client.post(
        INSTANCES,
        json={
            "agent_key": "nervos.chat",
            "agent_definition_version": "2",
            "display_name": name,
            "model_provider": PROVIDER_ID,
            "model_name": "opaque/model",
        },
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def trigger_body(agent_instance_id: int, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": "interval",
        "agent_instance_id": agent_instance_id,
        "display_name": "Poll",
        "input_text": "poll the source",
        "interval_seconds": 300,
    }
    body.update(overrides)
    return body


def created(client: TestClient, agent_instance_id: int, **overrides: Any) -> dict[str, Any]:
    response = client.post(
        TRIGGERS, json=trigger_body(agent_instance_id, **overrides), headers=ORIGIN
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def agent(owner_client: TestClient) -> int:
    return make_instance(owner_client)


# ------------------------------------------------------------------------------------------------
# Authority: the whole surface is authenticated, owner-scoped, and origin-protected
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", TRIGGERS, None),
        ("POST", TRIGGERS, {}),
        ("GET", f"{TRIGGERS}/1", None),
        ("PATCH", f"{TRIGGERS}/1", {}),
        ("POST", f"{TRIGGERS}/1/enable", None),
        ("POST", f"{TRIGGERS}/1/disable", None),
        ("DELETE", f"{TRIGGERS}/1", None),
        ("POST", f"{TRIGGERS}/1/rotate-secret", None),
        ("GET", f"{TRIGGERS}/1/occurrences", None),
    ],
)
def test_every_trigger_route_requires_authentication(
    client: TestClient, method: str, path: str, body: dict[str, Any] | None
) -> None:
    response = client.request(method, path, json=body, headers=ORIGIN)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


def test_mutations_require_the_exact_origin(owner_client: TestClient, agent: int) -> None:
    created_trigger = created(owner_client, agent)

    missing = owner_client.patch(
        f"{TRIGGERS}/{created_trigger['id']}",
        json={"expected_config_revision": 1, "display_name": "X"},
    )
    wrong = owner_client.patch(
        f"{TRIGGERS}/{created_trigger['id']}",
        json={"expected_config_revision": 1, "display_name": "X"},
        headers={"Origin": "http://evil.test"},
    )

    assert missing.status_code == 403
    assert missing.json()["error"]["code"] == "invalid_origin"
    assert wrong.status_code == 403


def test_the_new_body_bound_is_enforced(owner_client: TestClient, agent: int) -> None:
    oversized = b"{" + b"a" * (17 * 1024) + b"}"

    response = owner_client.post(
        TRIGGERS, content=oversized, headers={**ORIGIN, "Content-Type": "application/json"}
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


def test_a_non_json_content_type_is_still_refused(owner_client: TestClient) -> None:
    response = owner_client.post(
        TRIGGERS,
        content="kind=interval",
        headers={**ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"


# ------------------------------------------------------------------------------------------------
# Creation: every kind, and no schedule arithmetic in the transport
# ------------------------------------------------------------------------------------------------


def test_an_interval_trigger_is_created_enabled_with_a_future_instant(
    owner_client: TestClient, agent: int
) -> None:
    body = created(owner_client, agent)

    assert body["kind"] == "interval"
    assert body["enabled"] is True
    assert body["config_revision"] == 1
    assert body["interval_seconds"] == 300
    assert body["next_fire_at"] is not None
    assert body["input_text"] == "poll the source"


def test_an_event_trigger_carries_its_type_and_no_schedule(
    owner_client: TestClient, agent: int
) -> None:
    body = created(
        owner_client,
        agent,
        kind="event",
        interval_seconds=None,
        event_type="device.reading",
    )

    assert body["event_type"] == "device.reading"
    assert body["next_fire_at"] is None
    assert body["webhook_path"] is None


def test_a_one_time_trigger_keeps_the_instant_it_was_given(
    owner_client: TestClient, agent: int
) -> None:
    body = created(
        owner_client,
        agent,
        kind="one_time",
        interval_seconds=None,
        run_at="2030-01-01T00:00:00Z",
    )

    assert body["run_at"].startswith("2030-01-01")
    assert body["next_fire_at"].startswith("2030-01-01")


def test_a_cron_trigger_reports_its_expression_and_zone(
    owner_client: TestClient, agent: int
) -> None:
    body = created(
        owner_client,
        agent,
        kind="cron",
        interval_seconds=None,
        cron_expression="0 9 * * *",
        timezone="UTC",
    )

    assert body["cron_expression"] == "0 9 * * *"
    assert body["timezone"] == "UTC"
    assert body["next_fire_at"] is not None


def test_a_malformed_kind_shape_is_refused(owner_client: TestClient, agent: int) -> None:
    """A one_time trigger with an interval field is not a configuration, it is a contradiction."""
    response = owner_client.post(
        TRIGGERS,
        json=trigger_body(agent, kind="one_time", run_at="2030-01-01T00:00:00Z"),
        headers=ORIGIN,
    )

    assert response.status_code == 422


def test_server_fields_are_structurally_impossible_to_supply(
    owner_client: TestClient, agent: int
) -> None:
    for field, value in (
        ("id", 7),
        ("owner_user_id", 2),
        ("config_revision", 9),
        ("next_fire_at", "2030-01-01T00:00:00Z"),
        ("public_id", "a" * 22),
        ("secret", "hunter2"),
        ("secret_digest", "x"),
    ):
        response = owner_client.post(
            TRIGGERS, json={**trigger_body(agent), field: value}, headers=ORIGIN
        )
        assert response.status_code == 422, (field, response.text)


def test_an_unknown_agent_instance_target_is_a_not_found(owner_client: TestClient) -> None:
    response = owner_client.post(TRIGGERS, json=trigger_body(9999), headers=ORIGIN)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "agent_instance_not_found"


def test_a_disabled_creation_is_atomic(owner_client: TestClient, agent: int) -> None:
    body = created(owner_client, agent, enabled=False)

    assert body["enabled"] is False
    assert body["next_fire_at"] is None
