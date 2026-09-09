"""Adversarial HTTP-boundary tests for A3 authentication."""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false

from pathlib import Path

from fastapi import FastAPI
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
        "*",
        "not an origin",
        "http://other.test",
        "http://localhost:5174",
        "https://localhost:5173",
        "http://localhost:5173.attacker.test",
        "http://attacker.test/http://localhost:5173",
        "http://localhost:5173/",
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
    assert len(middleware) == 2
    assert any("AuthenticationBoundaryMiddleware" in repr(item) for item in middleware)
    assert any("ApiSecurityHeadersMiddleware" in repr(item) for item in middleware)


def test_api_responses_include_security_headers(client: TestClient) -> None:
    for response in (
        client.get("/api/v1/health"),
        client.get("/api/v1/setup/status"),
        client.get("/api/v1/missing"),
    ):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"


def test_credentials_require_json_content_type(client: TestClient) -> None:
    body = '{"username":"admin","password":"correct horse battery staple"}'
    for path in ("/api/v1/setup", "/api/v1/auth/login"):
        for content_type in (None, "text/plain", "application/x-www-form-urlencoded"):
            headers = dict(ORIGIN)
            if content_type is not None:
                headers["Content-Type"] = content_type
            response = client.post(path, headers=headers, content=body)
            assert response.status_code == 415
            assert response.json()["error"]["code"] == "unsupported_media_type"
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["x-content-type-options"] == "nosniff"


def test_oversized_credential_body_is_rejected_before_parsing(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/login",
        headers=ORIGIN,
        json={"username": "admin", "password": "x" * 5000},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


def test_password_work_limit_uses_safe_error_envelope(
    client: TestClient,
    migrated_app: tuple[FastAPI, Settings],
) -> None:
    from nervos_core.application.authentication import PasswordWorkLimit

    app, _ = migrated_app

    class BusyAuthenticationService:
        def login(self, username: str, password: str) -> None:
            del username, password
            raise PasswordWorkLimit

    app.state.authentication_service = BusyAuthenticationService()
    response = client.post(
        "/api/v1/auth/login",
        headers=ORIGIN,
        json={"username": "admin", "password": PASSWORD},
    )

    assert response.status_code == 429
    assert response.json() == {
        "error": {
            "code": "too_many_attempts",
            "message": "Too many authentication attempts.",
        }
    }
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "set-cookie" not in response.headers
    assert PASSWORD not in response.text


def test_production_unhandled_error_does_not_expose_stack_trace(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            environment="production",
            database_path=tmp_path / "production-errors.db",
            app_origin="https://example.test",
        )
    )

    def fail_safely() -> None:
        raise RuntimeError("SENSITIVE-STACK-SENTINEL")

    app.add_api_route("/api/v1/test-unhandled-error", fail_safely, methods=["GET"])

    with TestClient(app, raise_server_exceptions=False) as production_client:
        response = production_client.get("/api/v1/test-unhandled-error")

    assert response.status_code == 500
    assert "SENSITIVE-STACK-SENTINEL" not in response.text
    assert "Traceback" not in response.text
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_persistence_failures_use_safe_service_unavailable_envelope(
    client: TestClient,
    migrated_app: tuple[FastAPI, Settings],
) -> None:
    from nervos_core.application.authentication import PersistenceUnavailable

    app, _ = migrated_app

    class UnavailableAuthenticationService:
        def setup_is_complete(self) -> bool:
            raise PersistenceUnavailable

    app.state.authentication_service = UnavailableAuthenticationService()
    response = client.get("/api/v1/setup/status")

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "service_unavailable",
            "message": "Authentication is temporarily unavailable.",
        }
    }
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "set-cookie" not in response.headers
    assert "PersistenceUnavailable" not in response.text


def test_credential_body_size_boundary_rejects_missing_and_oversized() -> None:
    from nervos_api.api.middleware import AuthenticationBoundaryMiddleware

    assert AuthenticationBoundaryMiddleware.body_is_too_large(None) is True
    assert AuthenticationBoundaryMiddleware.body_is_too_large([]) is True
    assert AuthenticationBoundaryMiddleware.body_is_too_large([b"1", b"1"]) is True
    assert AuthenticationBoundaryMiddleware.body_is_too_large(b"not-an-int") is True
    assert AuthenticationBoundaryMiddleware.body_is_too_large(b"-1") is True
    assert AuthenticationBoundaryMiddleware.body_is_too_large(b"4097") is True
    assert AuthenticationBoundaryMiddleware.body_is_too_large(b"4096") is False
    assert AuthenticationBoundaryMiddleware.body_is_too_large(b"100") is False
