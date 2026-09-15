"""Integration tests for the public process-health endpoint."""

from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from nervos_api.app import create_app
from nervos_api.config import Settings
from nervos_core.application.model_providers import ModelProviderUnavailable


def test_app_starts_without_provider_credential_or_provider_network(tmp_path: Path) -> None:
    database_path = tmp_path / "no-provider.db"
    app = create_app(Settings(environment="test", database_path=database_path))
    with TestClient(app) as client:
        response = cast(
            Response,
            client.get("/api/v1/health"),  # pyright: ignore[reportUnknownMemberType]
        )
    assert response.status_code == 200
    # The control plane accepts work and executes nothing, and holds no configured provider.
    assert app.state.run_submission_service is not None
    assert app.state.model_provider_catalog.configured_ids == ()
    with pytest.raises(ModelProviderUnavailable):
        app.state.model_provider_catalog.resolve("anthropic")
    assert not database_path.exists()


def test_health_is_exact_liveness_response_without_database_access(tmp_path: Path) -> None:
    database_path = tmp_path / "health-must-not-create.db"
    app = create_app(Settings(environment="test", database_path=database_path))

    with TestClient(app) as client:
        response = cast(
            Response,
            client.get("/api/v1/health"),  # pyright: ignore[reportUnknownMemberType]
        )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.content == b'{"status":"ok"}'
    assert not database_path.exists()
