"""Integration tests for login, current-user, and logout endpoints."""

# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false

from fastapi import FastAPI
from fastapi.testclient import TestClient
from nervos_api.api.cookies import SESSION_COOKIE_NAME
from nervos_api.config import Settings
from nervos_core.infrastructure.database.models import AuthSessionRecord, UserRecord
from nervos_core.infrastructure.security.session_tokens import SecureSessionTokens
from sqlalchemy import select

ORIGIN = {"Origin": "http://localhost:5173"}
PASSWORD = "correct horse battery staple"


def setup_user(client: TestClient) -> str:
    response = client.post(
        "/api/v1/setup",
        headers=ORIGIN,
        json={"username": "admin", "password": PASSWORD},
    )
    assert response.status_code == 201
    token = client.cookies.get(SESSION_COOKIE_NAME)
    assert token is not None
    return token


def test_login_uses_safe_generic_errors_and_stores_only_digest(
    client: TestClient,
    migrated_app: tuple[FastAPI, Settings],
) -> None:
    app, _ = migrated_app
    setup_token = setup_user(client)
    client.cookies.clear()

    unknown = client.post(
        "/api/v1/auth/login",
        headers=ORIGIN,
        json={"username": "unknown", "password": PASSWORD},
    )
    wrong = client.post(
        "/api/v1/auth/login",
        headers=ORIGIN,
        json={"username": "admin", "password": "incorrect password"},
    )
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()
    assert unknown.json()["error"]["code"] == "invalid_credentials"

    success = client.post(
        "/api/v1/auth/login",
        headers=ORIGIN,
        json={"username": "ADMIN", "password": PASSWORD},
    )
    assert success.status_code == 200
    body = success.json()
    assert isinstance(body["id"], int)
    assert body == {
        "id": body["id"],
        "username": "admin",
        "role": "admin",
        "is_active": True,
    }
    login_token = client.cookies.get(SESSION_COOKIE_NAME)
    assert login_token is not None
    assert login_token != setup_token
    assert login_token not in success.text

    with app.state.session_factory() as session:
        sessions = session.scalars(select(AuthSessionRecord)).all()
        assert all(row.token_hash != login_token.encode() for row in sessions)
        assert any(row.token_hash == SecureSessionTokens().digest(login_token) for row in sessions)


def test_me_requires_valid_session_and_logout_revokes_current_session(
    client: TestClient,
) -> None:
    token = setup_user(client)

    assert client.get("/api/v1/auth/me").status_code == 200
    logout = client.post("/api/v1/auth/logout", headers=ORIGIN)
    assert logout.status_code == 204
    assert "Max-Age=0" in logout.headers["set-cookie"]
    assert client.get("/api/v1/auth/me").status_code == 401

    client.cookies.set(SESSION_COOKIE_NAME, token)
    assert client.get("/api/v1/auth/me").status_code == 401
    client.cookies.clear()
    repeated = client.post("/api/v1/auth/logout", headers=ORIGIN)
    assert repeated.status_code == 204


def test_inactive_user_cannot_login_or_use_existing_session(
    client: TestClient,
    migrated_app: tuple[FastAPI, Settings],
) -> None:
    app, _ = migrated_app
    token = setup_user(client)
    with app.state.session_factory.begin() as session:
        user = session.scalar(select(UserRecord))
        assert user is not None
        user.is_active = False

    assert client.get("/api/v1/auth/me").status_code == 401
    client.cookies.clear()
    response = client.post(
        "/api/v1/auth/login",
        headers=ORIGIN,
        json={"username": "admin", "password": PASSWORD},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"
    assert "set-cookie" not in response.headers
    assert token not in response.text
