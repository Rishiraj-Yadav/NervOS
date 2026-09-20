"""The public webhook ingress over the real application and the real middleware stack.

Nothing here calls a route function directly: every request goes through the composed ASGI app, so
the assertions cover the transport, the middleware ordering and the ingress together. That is what
makes the two halves of this file meaningful as a pair — the ingress works, **and** the browser
API's own protections are exactly what they were.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nervos_api.config import Settings
from nervos_core.application.agent_definitions import create_builtin_definition_registry
from nervos_core.application.webhooks import (
    WebhookDeliveryService,
    WebhookProvisioningService,
)
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.runs import TOOL_ENABLED_LIMITS
from nervos_core.infrastructure.database.jobs import SqlAlchemyJobPersistence
from nervos_core.infrastructure.database.triggers import SqlAlchemyTriggerPersistence
from nervos_core.infrastructure.webhooks import (
    RandomWebhookCredentialFactory,
    create_webhook_secret_verifier,
)
from sqlalchemy import text

INSTANCES = "/api/v1/agent-instances"
ORIGIN = {"Origin": "http://localhost:5173"}
PROVIDER_ID = "anthropic"
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)

#: A well-formed locator that names no trigger.
UNKNOWN_LOCATOR = "/hooks/v1/" + "a" * 22

JSON_BODY = b'{"event": "created"}'


def make_instance(client: TestClient) -> int:
    response = client.post(
        INSTANCES,
        json={
            "agent_key": "nervos.chat",
            "agent_definition_version": "2",
            "display_name": "Chat",
            "model_provider": PROVIDER_ID,
            "model_name": "opaque/model",
        },
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


class Issued:
    """One provisioned webhook, with the plaintext credential it was issued."""

    def __init__(self, locator: str, secret: str, trigger_id: int) -> None:
        self.locator = locator
        self.secret = secret
        self.trigger_id = trigger_id
        self.path = f"/hooks/v1/{locator}"

    def headers(self, **extra: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.secret}",
            "Content-Type": "application/json",
            **extra,
        }


@pytest.fixture
def issued(migrated_app: tuple[FastAPI, Settings], owner_client: TestClient) -> Issued:
    """A webhook trigger owned by the signed-in admin, created through the E3 services.

    Provisioning has no HTTP surface in E3, so the test composes the same application services the
    composition root will compose once E4 exposes them.
    """
    app, _ = migrated_app
    instance_id = make_instance(owner_client)
    persistence = SqlAlchemyTriggerPersistence(app.state.database_engine, sleep=lambda _: None)
    issued_webhook = WebhookProvisioningService(
        persistence, RandomWebhookCredentialFactory()
    ).create_webhook_trigger(
        owner_user_id=1,
        agent_instance_id=instance_id,
        display_name="On delivery",
        input_text="Handle the delivery.",
        now=NOW,
    )
    assert issued_webhook.trigger.public_id is not None
    return Issued(
        issued_webhook.trigger.public_id, issued_webhook.secret, issued_webhook.trigger.id
    )


def counts(app: FastAPI) -> dict[str, int]:
    with app.state.database_engine.connect() as connection:
        return {
            table: int(connection.scalar(text(f"SELECT COUNT(*) FROM {table}")) or 0)
            for table in ("runs", "jobs", "run_events", "trigger_occurrences")
        }


def post(
    client: TestClient,
    path: str,
    *,
    body: bytes = JSON_BODY,
    headers: dict[str, str] | None = None,
    content: bytes | None = None,
) -> Any:
    return client.post(
        path, content=content if content is not None else body, headers=headers or {}
    )


# ------------------------------------------------------------------------------------------------
# The route surface
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["get", "put", "patch", "delete"])
def test_the_ingress_exposes_exactly_one_post(owner_client: TestClient, method: str) -> None:
    response = getattr(owner_client, method)(UNKNOWN_LOCATOR)
    assert response.status_code == 405, response.text


def test_the_ingress_is_absent_from_the_openapi_document(owner_client: TestClient) -> None:
    document = owner_client.get("/openapi.json")
    assert document.status_code == 200
    assert "/hooks" not in document.text


def test_a_short_locator_is_refused_without_touching_the_database(owner_client: TestClient) -> None:
    response = post(owner_client, "/hooks/v1/short", headers={"Authorization": "Bearer x"})
    assert response.status_code == 404


# ------------------------------------------------------------------------------------------------
# The authentication collapse
# ------------------------------------------------------------------------------------------------


def test_every_authentication_failure_is_one_indistinguishable_response(
    owner_client: TestClient, issued: Issued
) -> None:
    """Same status, same body, same headers -- so probing learns nothing about which exist."""
    responses = [
        post(owner_client, UNKNOWN_LOCATOR, headers=issued.headers()),
        post(owner_client, UNKNOWN_LOCATOR),
        post(owner_client, issued.path),
        post(owner_client, issued.path, headers={"Authorization": "Bearer " + "b" * 43}),
        post(owner_client, issued.path, headers={"Authorization": "Basic " + issued.secret}),
        post(owner_client, issued.path, headers={"Authorization": "Bearer "}),
        post(owner_client, "/hooks/v1/" + "!" * 22, headers=issued.headers()),
    ]

    statuses = {response.status_code for response in responses}
    bodies = {response.text for response in responses}
    assert statuses == {404}, statuses
    assert len(bodies) == 1, bodies
    assert json.loads(bodies.pop()) == {
        "code": "not_found",
        "message": "The requested webhook is not available.",
    }


def test_two_authorization_headers_are_refused_rather_than_resolved(
    owner_client: TestClient, issued: Issued
) -> None:
    response = owner_client.post(
        issued.path,
        content=JSON_BODY,
        headers=[
            ("Authorization", f"Bearer {issued.secret}"),
            ("Authorization", "Bearer " + "b" * 43),
            ("Content-Type", "application/json"),
        ],
    )
    assert response.status_code == 404


# ------------------------------------------------------------------------------------------------
# The accepted path
# ------------------------------------------------------------------------------------------------


def test_a_valid_delivery_is_accepted_and_creates_an_ordinary_run(
    owner_client: TestClient, issued: Issued
) -> None:
    response = post(owner_client, issued.path, headers=issued.headers())

    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["duplicate"] is False
    assert payload["code"] is None
    assert isinstance(payload["occurrence_id"], int)
    assert isinstance(payload["run_id"], int)
    assert set(payload) == {"duplicate", "occurrence_id", "run_id", "code"}


def test_a_repeated_key_is_answered_as_a_duplicate_without_a_second_run(
    owner_client: TestClient, issued: Issued, app_under_test: FastAPI
) -> None:
    headers = issued.headers(**{"Idempotency-Key": "key-1"})
    first = post(owner_client, issued.path, headers=headers)
    second = post(owner_client, issued.path, headers=headers)

    assert first.status_code == second.status_code == 202
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    assert second.json()["occurrence_id"] == first.json()["occurrence_id"]
    assert counts(app_under_test)["trigger_occurrences"] == 1
    assert counts(app_under_test)["runs"] == 1


def test_the_same_key_with_different_bytes_conflicts(
    owner_client: TestClient, issued: Issued, app_under_test: FastAPI
) -> None:
    headers = issued.headers(**{"Idempotency-Key": "key-1"})
    assert post(owner_client, issued.path, body=b'{"a": 1}', headers=headers).status_code == 202

    response = post(owner_client, issued.path, body=b'{"a": 2}', headers=headers)

    assert response.status_code == 409
    assert response.json()["code"] == "idempotency_conflict"
    assert counts(app_under_test)["trigger_occurrences"] == 1


def test_a_disabled_trigger_is_refused_only_after_authentication(
    owner_client: TestClient, issued: Issued, app_under_test: FastAPI
) -> None:
    with app_under_test.state.database_engine.begin() as connection:
        connection.execute(
            text("UPDATE trigger_definitions SET enabled = 0 WHERE id = :id"),
            {"id": issued.trigger_id},
        )

    authenticated = post(owner_client, issued.path, headers=issued.headers())
    unauthenticated = post(owner_client, issued.path)

    assert authenticated.status_code == 403
    assert authenticated.json()["code"] == "webhook_unavailable"
    # A caller who cannot authenticate cannot learn that the trigger exists but is disabled.
    assert unauthenticated.status_code == 404
    assert counts(app_under_test)["trigger_occurrences"] == 0


def test_an_unavailable_agent_is_reported_as_a_static_public_code(
    owner_client: TestClient, issued: Issued, app_under_test: FastAPI
) -> None:
    with app_under_test.state.database_engine.begin() as connection:
        connection.execute(text("UPDATE agent_instances SET enabled = 0"))

    response = post(owner_client, issued.path, headers=issued.headers())

    assert response.status_code == 202
    assert response.json()["run_id"] is None
    assert response.json()["code"] == "agent_disabled"
    assert counts(app_under_test)["runs"] == 0


# ------------------------------------------------------------------------------------------------
# Body, content type and idempotency key
# ------------------------------------------------------------------------------------------------


def test_a_non_json_content_type_is_refused_after_authentication(
    owner_client: TestClient, issued: Issued
) -> None:
    response = post(
        owner_client,
        issued.path,
        headers={"Authorization": f"Bearer {issued.secret}", "Content-Type": "text/plain"},
    )

    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_media_type"


def test_a_missing_content_type_is_refused(owner_client: TestClient, issued: Issued) -> None:
    response = post(owner_client, issued.path, headers={"Authorization": f"Bearer {issued.secret}"})
    assert response.status_code == 415


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"{",
        b"[]",
        b"1",
        b"null",
        b'"text"',
        b"\xff\xfe\x00",
        b'{"a": 1, "a": 2}',
        b'{"a": NaN}',
    ],
)
def test_a_body_outside_the_frozen_contract_is_refused(
    owner_client: TestClient, issued: Issued, body: bytes
) -> None:
    response = post(owner_client, issued.path, body=body or b" ", headers=issued.headers())
    assert response.status_code in {400, 415}, response.text
    if response.status_code == 400:
        assert response.json()["code"] == "malformed_payload"


def test_a_body_over_the_frozen_bound_is_refused_by_the_transport(
    owner_client: TestClient, issued: Issued, app_under_test: FastAPI
) -> None:
    oversized = b'{"a": "' + b"x" * 70_000 + b'"}'

    response = post(owner_client, issued.path, body=oversized, headers=issued.headers())

    assert response.status_code == 413
    assert response.json()["code"] == "request_too_large"
    assert counts(app_under_test)["trigger_occurrences"] == 0


def test_an_oversized_declared_length_is_refused_without_reading_the_body(
    owner_client: TestClient, issued: Issued
) -> None:
    """The declared length is not trusted, but an obviously impossible one is cheap to refuse."""
    response = post(
        owner_client,
        issued.path,
        body=b"{}",
        headers=issued.headers(**{"Content-Length": "999999"}),
    )
    assert response.status_code == 413


def test_a_malformed_idempotency_key_is_refused(owner_client: TestClient, issued: Issued) -> None:
    response = post(
        owner_client, issued.path, headers=issued.headers(**{"Idempotency-Key": "has space"})
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_idempotency_key"


def test_conflicting_idempotency_keys_are_refused(owner_client: TestClient, issued: Issued) -> None:
    response = owner_client.post(
        issued.path,
        content=JSON_BODY,
        headers=[
            ("Authorization", f"Bearer {issued.secret}"),
            ("Content-Type", "application/json"),
            ("Idempotency-Key", "one"),
            ("Idempotency-Key", "two"),
        ],
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_idempotency_key"


def test_admission_backpressure_is_retryable_and_records_nothing(
    owner_client: TestClient, issued: Issued, app_under_test: FastAPI
) -> None:
    """A cap of one, one occupying Run, and a delivery that must therefore be deferred intact."""
    engine = app_under_test.state.database_engine
    app_under_test.state.webhook_ingress_service = WebhookDeliveryService(
        SqlAlchemyTriggerPersistence(engine, max_pending=1),
        create_builtin_definition_registry(),
        create_webhook_secret_verifier(),
    )
    SqlAlchemyJobPersistence(engine, max_pending=1000).submit(
        owner_user_id=1,
        agent_instance_id=1,
        input_text="occupy the only slot",
        limits=TOOL_ENABLED_LIMITS,
        definition_id=AgentDefinitionId("nervos.chat", "2"),
        now=NOW,
    )

    response = post(owner_client, issued.path, headers=issued.headers())

    assert response.status_code == 503
    assert response.json()["code"] == "service_unavailable"
    assert response.headers["retry-after"] == "1"
    assert counts(app_under_test)["trigger_occurrences"] == 0


# ------------------------------------------------------------------------------------------------
# Response hygiene
# ------------------------------------------------------------------------------------------------


def test_every_response_carries_the_ingress_security_headers(
    owner_client: TestClient, issued: Issued
) -> None:
    responses = [
        post(owner_client, issued.path, headers=issued.headers()),
        post(owner_client, issued.path),
        post(owner_client, issued.path, headers=issued.headers(), body=b"{"),
        post(owner_client, issued.path, headers=issued.headers(), body=b"x" * 70_000),
    ]

    for response in responses:
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"


def test_no_response_names_anything_internal(
    owner_client: TestClient, issued: Issued, app_under_test: FastAPI
) -> None:
    """The published shape is an allow-list: a stored row can never widen it."""
    accepted = post(owner_client, issued.path, headers=issued.headers())
    body = accepted.text

    for forbidden in (
        "owner_user_id",
        "agent_instance_id",
        "agent_definition",
        "job_id",
        "attempt",
        "claim_token",
        "lease",
        "worker",
        "partition",
        "grant",
        "secret",
        "payload",
        "idempotency_key",
        str(issued.secret),
    ):
        assert forbidden not in body, forbidden
    assert issued.secret not in accepted.headers.get("authorization", "")
    assert "location" not in {key.lower() for key in accepted.headers}


def test_no_redirect_is_ever_issued(owner_client: TestClient, issued: Issued) -> None:
    for path in (issued.path, UNKNOWN_LOCATOR, "/hooks/v1/short"):
        assert post(owner_client, path, headers=issued.headers()).status_code not in {
            301,
            302,
            307,
            308,
        }


def test_the_secret_never_reaches_the_log(
    owner_client: TestClient, issued: Issued, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        post(owner_client, issued.path, headers=issued.headers())
        post(owner_client, issued.path, headers={"Authorization": f"Bearer {issued.secret}"})
        post(owner_client, issued.path)

    assert issued.secret not in caplog.text
    assert "Bearer" not in caplog.text


# ------------------------------------------------------------------------------------------------
# The boundary in both directions
# ------------------------------------------------------------------------------------------------


def test_a_session_cookie_is_not_a_webhook_credential(
    owner_client: TestClient, issued: Issued
) -> None:
    """The signed-in client's own cookie authenticates nothing on the ingress."""
    assert owner_client.cookies, "the fixture must be authenticated for this to mean anything"

    response = post(owner_client, issued.path)

    assert response.status_code == 404


def test_a_webhook_secret_does_not_authorize_the_management_api(
    app_under_test: FastAPI, issued: Issued
) -> None:
    """A second, credential-free client: the management API takes a cookie, and only a cookie."""
    anonymous = TestClient(app_under_test)

    unauthenticated = anonymous.get(INSTANCES)
    with_secret = anonymous.get(INSTANCES, headers={"Authorization": f"Bearer {issued.secret}"})

    assert unauthenticated.status_code == 401
    assert with_secret.status_code == 401


def test_the_management_api_keeps_its_origin_and_body_protections(
    owner_client: TestClient, issued: Issued
) -> None:
    """The webhook namespace is a sibling, so every browser-API protection is unchanged."""
    without_origin = owner_client.post(
        INSTANCES,
        json={
            "agent_key": "nervos.chat",
            "agent_definition_version": "2",
            "display_name": "X",
            "model_provider": PROVIDER_ID,
            "model_name": "m",
        },
    )
    wrong_origin = owner_client.post(
        INSTANCES,
        json={
            "agent_key": "nervos.chat",
            "agent_definition_version": "2",
            "display_name": "X",
            "model_provider": PROVIDER_ID,
            "model_name": "m",
        },
        headers={"Origin": "http://evil.example"},
    )

    assert without_origin.status_code == 403
    assert without_origin.json()["error"]["code"] == "invalid_origin"
    assert wrong_origin.status_code == 403

    oversized = owner_client.post(
        INSTANCES,
        content=b"x" * 20_000,
        headers={**ORIGIN, "Content-Type": "application/json"},
    )
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "request_too_large"

    wrong_type = owner_client.post(
        INSTANCES, content=b"x", headers={**ORIGIN, "Content-Type": "text/plain"}
    )
    assert wrong_type.status_code == 415
    assert wrong_type.json()["error"]["code"] == "unsupported_media_type"
