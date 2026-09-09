"""Integration tests for first-run setup."""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false

from fastapi import FastAPI
from fastapi.testclient import TestClient
from nervos_api.api.cookies import SESSION_COOKIE_NAME
from nervos_api.config import Settings
from nervos_core.infrastructure.database.models import AuthSessionRecord, UserRecord
from sqlalchemy import func, select

ORIGIN = {"Origin": "http://localhost:5173"}
PASSWORD = "correct horse battery staple"


def test_setup_creates_admin_sets_cookie_and_locks_setup(client: TestClient) -> None:
    initial_status = client.get("/api/v1/setup/status")
    assert initial_status.status_code == 200
    assert initial_status.json() == {"setup_complete": False}
    assert initial_status.headers["cache-control"] == "no-store"

    response = client.post(
        "/api/v1/setup",
        headers=ORIGIN,
        json={"username": "  Admin.User ", "password": PASSWORD},
    )

    assert response.status_code == 201
    body = response.json()
    assert isinstance(body["id"], int)
    assert body == {
        "id": body["id"],
        "username": "admin.user",
        "role": "admin",
        "is_active": True,
    }
    assert PASSWORD not in response.text
    assert "password_hash" not in response.text
    assert SESSION_COOKIE_NAME not in response.json()
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/" in cookie
    assert "Max-Age=604800" in cookie
    assert "Secure" not in cookie
    assert response.headers["cache-control"] == "no-store"

    me_response = client.get("/api/v1/auth/me")
    assert me_response.status_code == 200
    assert me_response.json() == response.json()

    repeated = client.post(
        "/api/v1/setup",
        headers=ORIGIN,
        json={"username": "second-user", "password": PASSWORD},
    )
    assert repeated.status_code == 409
    assert repeated.json()["error"]["code"] == "setup_complete"
    assert client.get("/api/v1/setup/status").json() == {"setup_complete": True}
    assert client.get("/api/v1/setup/admin").status_code == 404


def test_setup_rejects_invalid_or_missing_origin_without_mutation(
    client: TestClient,
    migrated_app: tuple[FastAPI, Settings],
) -> None:
    app, _ = migrated_app
    payload = {"username": "admin", "password": PASSWORD}

    missing = client.post("/api/v1/setup", json=payload)
    wrong = client.post(
        "/api/v1/setup",
        headers={"Origin": "http://localhost:5173.attacker.test"},
        json=payload,
    )

    assert missing.status_code == 403
    assert wrong.status_code == 403
    with app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(UserRecord)) == 0
        assert session.scalar(select(func.count()).select_from(AuthSessionRecord)) == 0


def test_validation_response_never_echoes_password(client: TestClient) -> None:
    sentinel = "SENTINEL-PLAINTEXT-PASSWORD"
    response = client.post(
        "/api/v1/setup",
        headers=ORIGIN,
        json={"username": {"unexpected": "shape"}, "password": sentinel},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert sentinel not in response.text
