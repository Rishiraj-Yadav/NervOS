"""Adversarial HTTP-boundary tests for A3 authentication."""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false

from pathlib import Path

from fastapi.testclient import TestClient
from nervos_api.api.cookies import SESSION_COOKIE_NAME
from nervos_api.app import create_app
from nervos_api.config import Settings

ORIGIN = {"Origin": "http://localhost:5173"}
PASSWORD = "correct horse battery staple"


def test_auth_mutations_require_exact_origin(client: TestClient) -> None:
    payload = {"username": "admin", "password": PASSWORD}
    invalid_origins = [
        None,
        "null",
        "http://localhost:5174",
        "https://localhost:5173",
        "http://attacker.test/http://localhost:5173",
    ]
    for path in ("/api/v1/setup", "/api/v1/auth/login", "/api/v1/auth/logout"):
        for origin in invalid_origins:
            headers = {} if origin is None else {"Origin": origin}
            response = client.post(path, headers=headers, json=payload)
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "invalid_origin"


def test_invalid_cookie_is_safe_and_health_remains_public(client: TestClient) -> None:
    client.cookies.set(SESSION_COOKIE_NAME, "malformed")
    response = client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "authentication_required",
            "message": "Authentication is required.",
        }
    }
    assert client.get("/api/v1/health").json() == {"status": "ok"}


def test_secure_cookie_is_derived_from_https_and_production(tmp_path: Path) -> None:
    development_https = Settings(
        environment="development",
        database_path=tmp_path / "development.db",
        app_origin="https://localhost:5173",
    )
    production = Settings(
        environment="production",
        database_path=tmp_path / "production.db",
        app_origin="https://example.test",
    )

    from nervos_api.api.cookies import cookie_is_secure

    assert cookie_is_secure(development_https) is True
    assert cookie_is_secure(production) is True
    middleware = create_app(development_https).user_middleware
    assert len(middleware) == 1
    assert "AuthenticationBoundaryMiddleware" in repr(middleware[0])


def test_oversized_credential_body_is_rejected_before_parsing(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/login",
        headers=ORIGIN,
        json={"username": "admin", "password": "x" * 5000},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


def test_credential_body_size_boundary_rejects_missing_and_oversized() -> None:
    from nervos_api.api.middleware import AuthenticationBoundaryMiddleware

    assert AuthenticationBoundaryMiddleware.body_is_too_large(None) is True
    assert AuthenticationBoundaryMiddleware.body_is_too_large(b"not-an-int") is True
    assert AuthenticationBoundaryMiddleware.body_is_too_large(b"5000") is True
    assert AuthenticationBoundaryMiddleware.body_is_too_large(b"100") is False
