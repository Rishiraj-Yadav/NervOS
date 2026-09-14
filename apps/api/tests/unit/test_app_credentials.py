"""Composition tests proving a provider credential never outlives client construction.

A credential is read exactly once, locally, to build its provider client. These tests assert
that the composed application still receives that credential, while nothing reachable
afterwards - app state, the public API surface, or the log - carries it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from nervos_api.app import create_app
from nervos_api.config import Settings
from pydantic import SecretStr

ANTHROPIC_CREDENTIAL = "SYNTHETIC-ANTHROPIC-APP-CREDENTIAL"
OPENAI_CREDENTIAL = "SYNTHETIC-OPENAI-APP-CREDENTIAL"
CREDENTIALS = (ANTHROPIC_CREDENTIAL, OPENAI_CREDENTIAL)
PROVIDER_IDS = ("anthropic", "openai")

ComposeApp = Callable[[str | None, str | None], FastAPI]


def _secret(value: str | None) -> SecretStr | None:
    """Wrap a synthetic credential the way process configuration does."""
    return SecretStr(value) if value is not None else None


@pytest.fixture(autouse=True)
def clear_provider_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a real process credential reach a composed application."""
    for variable in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture
def compose_app(tmp_path: Path) -> Iterator[ComposeApp]:
    """Compose applications over a disposable database path and dispose them afterwards."""
    composed: list[FastAPI] = []

    def compose(anthropic: str | None, openai: str | None) -> FastAPI:
        settings = Settings(
            environment="test",
            database_path=tmp_path / "app-credentials.db",
            anthropic_api_key=_secret(anthropic),
            openai_api_key=_secret(openai),
        )
        app = create_app(settings)
        composed.append(app)
        return app

    yield compose

    for app in composed:
        app.state.database_engine.dispose()


@pytest.mark.parametrize(
    ("anthropic", "openai"),
    [
        (None, None),
        (ANTHROPIC_CREDENTIAL, None),
        (None, OPENAI_CREDENTIAL),
        (ANTHROPIC_CREDENTIAL, OPENAI_CREDENTIAL),
    ],
)
def test_every_credential_combination_composes_without_credentials_in_app_state(
    compose_app: ComposeApp, anthropic: str | None, openai: str | None
) -> None:
    app = compose_app(anthropic, openai)
    catalog = app.state.model_provider_catalog

    for provider_id in PROVIDER_IDS:
        assert catalog.is_known(provider_id)
    assert catalog.is_configured("anthropic") is (anthropic is not None)
    assert catalog.is_configured("openai") is (openai is not None)
    assert app.state.settings.anthropic_api_key is None
    assert app.state.settings.openai_api_key is None


def test_composed_providers_receive_the_credentials_that_app_state_no_longer_holds(
    compose_app: ComposeApp,
) -> None:
    """Composition must still be configured from the credential, then drop it."""
    app = compose_app(ANTHROPIC_CREDENTIAL, OPENAI_CREDENTIAL)

    assert app.state.model_provider_catalog.is_configured("anthropic")
    assert app.state.model_provider_catalog.is_configured("openai")
    assert app.state.settings.anthropic_api_key is None
    assert app.state.settings.openai_api_key is None


def test_no_credential_is_reachable_from_app_state_repr_or_serialization(
    compose_app: ComposeApp,
) -> None:
    app = compose_app(ANTHROPIC_CREDENTIAL, OPENAI_CREDENTIAL)

    state = vars(app.state)
    for credential in CREDENTIALS:
        assert credential not in repr(state)
        assert credential not in repr(app.state.settings)
        assert credential not in str(app.state.settings.model_dump())
        assert credential not in repr(app.state.model_provider_catalog)


def test_no_credential_appears_in_the_public_api_surface(compose_app: ComposeApp) -> None:
    app = compose_app(ANTHROPIC_CREDENTIAL, OPENAI_CREDENTIAL)

    document = str(app.openapi())

    for credential in CREDENTIALS:
        assert credential not in document


def test_composition_logs_nothing_about_a_credential(
    compose_app: ComposeApp, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        app = compose_app(ANTHROPIC_CREDENTIAL, OPENAI_CREDENTIAL)

    assert app.state.model_provider_catalog.is_configured("anthropic")
    assert app.state.model_provider_catalog.is_configured("openai")
    for credential in CREDENTIALS:
        assert credential not in caplog.text
