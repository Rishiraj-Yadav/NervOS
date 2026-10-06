"""Stage-H control-plane route tests: secrets, connections, approvals, publisher trust.

These assert the public API contract only: what an owner may do, what is refused, and -- the
part that matters most -- that no response body and no stored row ever carries a secret value.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nervos_api.api.cookies import SESSION_COOKIE_NAME
from nervos_api.app import create_app
from nervos_api.config import Settings
from sqlalchemy import Engine, text

ORIGIN = {"Origin": "http://localhost:5173"}
SECRET_VALUE = "super-secret-token-value"
FINGERPRINT = "a" * 64


@pytest.fixture
def secrets_key_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the composed app at a usable master key file before it is composed.

    Declared first in every consuming signature so this resolves before ``migrated_app``:
    settings read the environment at construction, so the variable must exist by then.
    """
    key_file = tmp_path / "secrets.key"
    key_file.write_text(base64.b64encode(os.urandom(32)).decode("ascii"), encoding="utf-8")
    # The resolver enforces owner-only access on POSIX, so the fixture provisions it that way.
    os.chmod(key_file, 0o600)
    monkeypatch.setenv("NERVOS_SECRETS_KEY_FILE", str(key_file))
    yield key_file


@pytest.fixture
def secured_client(secrets_key_file: Path, owner_client: TestClient) -> TestClient:
    del secrets_key_file
    return owner_client


@pytest.fixture
def database_engine(migrated_app: tuple[FastAPI, Settings]) -> Engine:
    """The composed engine, for at-rest assertions about stored rows."""
    app, _ = migrated_app
    engine: Engine = app.state.database_engine
    return engine


@pytest.fixture
def closed_key_client(
    migrated_app: tuple[FastAPI, Settings], tmp_path: Path, owner_client: TestClient
) -> Iterator[TestClient]:
    """The same database and identity, composed with a master key file that does not exist.

    The session cookie is copied rather than re-created: sessions are server-side rows, so the
    same database means the same authenticated user, which isolates the only variable under
    test -- the unusable key.
    """
    _, settings = migrated_app
    broken = create_app(settings.model_copy(update={"secrets_key_file": tmp_path / "absent.key"}))
    with TestClient(broken) as client:
        token = owner_client.cookies.get(SESSION_COOKIE_NAME)
        assert token is not None
        client.cookies.set(SESSION_COOKIE_NAME, token)
        yield client


def create_secret(client: TestClient, name: str = "github-token") -> dict[str, object]:
    response = client.post(
        "/api/v1/secrets",
        json={"name": name, "value": SECRET_VALUE, "provider_hint": "github"},
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    payload: dict[str, object] = response.json()
    return payload


def create_connection(client: TestClient, secret_id: object) -> dict[str, object]:
    response = client.post(
        "/api/v1/account-connections",
        json={
            "provider": "github",
            "display_name": "GitHub",
            "secret_id": secret_id,
            "scopes": ["repo:read"],
        },
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    payload: dict[str, object] = response.json()
    return payload


def test_secrets_round_trip_never_returns_the_value(secured_client: TestClient) -> None:
    created = create_secret(secured_client)

    assert created["name"] == "github-token"
    assert created["status"] == "active"
    assert SECRET_VALUE not in secured_client.get("/api/v1/secrets").text
    assert SECRET_VALUE not in secured_client.get(f"/api/v1/secrets/{created['id']}").text

    replaced = secured_client.put(
        f"/api/v1/secrets/{created['id']}/value",
        json={"value": "rotated-value"},
        headers=ORIGIN,
    )
    assert replaced.status_code == 200
    assert "rotated-value" not in replaced.text
    assert replaced.json()["rotation_count"] == 1


def test_a_secret_is_encrypted_at_rest(secured_client: TestClient, database_engine: Engine) -> None:
    created = create_secret(secured_client)

    with database_engine.connect() as connection:
        rows = (
            connection.execute(
                text("SELECT name, ciphertext FROM secrets WHERE id = :secret_id"),
                {"secret_id": created["id"]},
            )
            .mappings()
            .all()
        )

    assert rows and SECRET_VALUE.encode("utf-8") not in rows[0]["ciphertext"]
    assert rows[0]["name"] == "github-token"


def test_secret_lifecycle_states_and_missing_secret(secured_client: TestClient) -> None:
    created = create_secret(secured_client)
    secret_id = created["id"]

    disabled = secured_client.put(
        f"/api/v1/secrets/{secret_id}/status", json={"status": "disabled"}, headers=ORIGIN
    )
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"

    revoked = secured_client.put(
        f"/api/v1/secrets/{secret_id}/status", json={"status": "revoked"}, headers=ORIGIN
    )
    assert revoked.json()["status"] == "revoked"

    missing = secured_client.get("/api/v1/secrets/999999")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "secret_not_found"


def test_a_referenced_secret_cannot_be_revoked(secured_client: TestClient) -> None:
    created = create_secret(secured_client)
    create_connection(secured_client, created["id"])

    revoked = secured_client.put(
        f"/api/v1/secrets/{created['id']}/status", json={"status": "revoked"}, headers=ORIGIN
    )
    assert revoked.status_code == 409
    assert revoked.json()["error"]["code"] == "secret_referenced"


def test_account_connections_are_listed_and_disconnectable(secured_client: TestClient) -> None:
    created = create_secret(secured_client)
    connection = create_connection(secured_client, created["id"])
    assert connection["state"] == "connected"
    assert connection["scopes"] == ["repo:read"]
    assert SECRET_VALUE not in secured_client.get("/api/v1/account-connections").text

    listed = secured_client.get("/api/v1/account-connections")
    assert [item["id"] for item in listed.json()] == [connection["id"]]

    disconnected = secured_client.post(
        f"/api/v1/account-connections/{connection['id']}/disconnect", headers=ORIGIN
    )
    assert disconnected.status_code == 204
    assert (
        secured_client.get(f"/api/v1/account-connections/{connection['id']}").json()["state"]
        == "disconnected"
    )


def test_publisher_trust_records_a_local_revocation(secured_client: TestClient) -> None:
    response = secured_client.put(
        f"/api/v1/publisher-trust/{FINGERPRINT}",
        json={"state": "revoked", "reason": "compromised key"},
        headers=ORIGIN,
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "revoked"

    listed = secured_client.get("/api/v1/publisher-trust")
    assert [item["signer_fingerprint"] for item in listed.json()] == [FINGERPRINT]

    restored = secured_client.put(
        f"/api/v1/publisher-trust/{FINGERPRINT}", json={"state": "trusted"}, headers=ORIGIN
    )
    assert restored.json()["state"] == "trusted"


def test_approval_decisions_are_owner_scoped(secured_client: TestClient) -> None:
    assert secured_client.get("/api/v1/action-approvals").json() == []
    unknown = secured_client.post(
        "/api/v1/action-approvals/4242/decision", json={"approve": True}, headers=ORIGIN
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "action_approval_not_found"


def test_secret_routes_require_authentication(client: TestClient) -> None:
    assert client.get("/api/v1/secrets").status_code == 401


def test_an_unusable_master_key_fails_closed(closed_key_client: TestClient) -> None:
    response = closed_key_client.post(
        "/api/v1/secrets", json={"name": "late", "value": SECRET_VALUE}, headers=ORIGIN
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "secret_store_unavailable"
