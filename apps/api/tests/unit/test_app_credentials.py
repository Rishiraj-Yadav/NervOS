"""Composition tests proving the control plane never holds a provider credential.

Execution is a Worker capability, so the API reads no provider credential at all. These tests
compose the real application with *both* credentials present in the process environment - the
strongest form of the property: the control plane holds no key even when one is available to
it, and it constructs no credential-bearing client to hold one on its behalf.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from nervos_api.app import create_app
from nervos_api.config import Settings

ANTHROPIC_CREDENTIAL = "SYNTHETIC-ANTHROPIC-APP-CREDENTIAL"
OPENAI_CREDENTIAL = "SYNTHETIC-OPENAI-APP-CREDENTIAL"
CREDENTIALS = (ANTHROPIC_CREDENTIAL, OPENAI_CREDENTIAL)
PROVIDER_IDS = ("anthropic", "openai")


@pytest.fixture
def compose_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    """Compose the real application with both provider credentials exported."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_CREDENTIAL)
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_CREDENTIAL)
    app = create_app(Settings(environment="test", database_path=tmp_path / "no-credentials.db"))
    yield app
    app.state.database_engine.dispose()


def test_the_api_knows_the_supported_providers_and_configures_none(compose_app: FastAPI) -> None:
    catalog = compose_app.state.model_provider_catalog

    for provider_id in PROVIDER_IDS:
        assert catalog.is_known(provider_id)
        assert catalog.is_configured(provider_id) is False
    assert catalog.configured_ids == ()


def test_no_credential_is_reachable_from_app_state_repr_or_serialization(
    compose_app: FastAPI,
) -> None:
    state = vars(compose_app.state)

    for credential in CREDENTIALS:
        assert credential not in repr(state)
        assert credential not in repr(compose_app.state.settings)
        assert credential not in str(compose_app.state.settings.model_dump())
        assert credential not in repr(compose_app.state.model_provider_catalog)


def test_no_credential_bearing_client_is_reachable(compose_app: FastAPI) -> None:
    """Nothing credential-bearing may outlive composition, not even unused."""
    for name, value in vars(compose_app.state).items():
        assert "anthropic" not in name.lower(), name
        assert "openai" not in name.lower(), name
        assert type(value).__module__.split(".")[0] not in {"anthropic", "openai"}, name


def test_no_credential_appears_in_the_public_api_surface(compose_app: FastAPI) -> None:
    document = str(compose_app.openapi())

    for credential in CREDENTIALS:
        assert credential not in document


def test_composition_logs_nothing_about_a_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_CREDENTIAL)
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_CREDENTIAL)

    with caplog.at_level(logging.DEBUG):
        app = create_app(Settings(environment="test", database_path=tmp_path / "logs.db"))
    try:
        for credential in CREDENTIALS:
            assert credential not in caplog.text
    finally:
        app.state.database_engine.dispose()
