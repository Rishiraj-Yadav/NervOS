"""Exact model-provider resolution and availability tests."""

import pytest
from nervos_core.application.model_completion import (
    ModelCompletion,
    ModelRequest,
    ModelResponse,
    StopOutcome,
)
from nervos_core.application.model_providers import (
    DuplicateModelProvider,
    ModelProviderCatalog,
    ModelProviderUnavailable,
    UnknownModelProvider,
)


class _RecordingCompletion:
    def __init__(self) -> None:
        self.calls: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:  # pragma: no cover
        self.calls.append(request)
        return ModelResponse("unused", "anthropic", "opaque/model", StopOutcome.STOP)


def _catalog() -> tuple[ModelProviderCatalog, _RecordingCompletion]:
    completion = _RecordingCompletion()
    catalog = ModelProviderCatalog([("anthropic", lambda: completion)], known=["anthropic"])
    return catalog, completion


def test_catalog_factories_satisfy_the_model_port() -> None:
    catalog, completion = _catalog()
    resolved: ModelCompletion = catalog.resolve("anthropic")
    assert resolved is completion


def test_unknown_provider_is_rejected() -> None:
    catalog, _ = _catalog()
    assert catalog.is_known("openai") is False
    with pytest.raises(UnknownModelProvider):
        catalog.resolve("openai")


def test_known_provider_without_configuration_is_known_but_unavailable() -> None:
    catalog = ModelProviderCatalog([], known=["anthropic"])
    assert catalog.is_known("anthropic") is True
    assert catalog.is_configured("anthropic") is False
    with pytest.raises(ModelProviderUnavailable):
        catalog.resolve("anthropic")


def test_configured_provider_resolves_exactly() -> None:
    catalog, completion = _catalog()
    assert catalog.is_known("anthropic") is True
    assert catalog.is_configured("anthropic") is True
    assert catalog.resolve("anthropic") is completion


def test_duplicate_registration_fails() -> None:
    first = _RecordingCompletion()
    second = _RecordingCompletion()
    with pytest.raises(DuplicateModelProvider):
        ModelProviderCatalog(
            [("anthropic", lambda: first), ("anthropic", lambda: second)],
            known=["anthropic"],
        )


def test_registered_identifier_conflicting_with_duplicate_registration_fails() -> None:
    with pytest.raises(DuplicateModelProvider):
        ModelProviderCatalog(
            [("anthropic", _RecordingCompletion), ("anthropic", _RecordingCompletion)]
        )


def test_registered_identifier_is_also_known() -> None:
    catalog = ModelProviderCatalog([("anthropic", _RecordingCompletion)])
    assert catalog.is_known("anthropic") is True
    assert catalog.is_configured("anthropic") is True


@pytest.mark.parametrize(
    "candidate", ["Anthropic", "ANTHROPIC", "claude", "anthropic ", "anthropic.alt", ""]
)
def test_lookup_is_exact_case_sensitive_and_has_no_alias_or_fallback(candidate: str) -> None:
    catalog, _ = _catalog()
    with pytest.raises(UnknownModelProvider):
        catalog.resolve(candidate)


def test_unrelated_identifier_is_unknown_and_unconfigured() -> None:
    catalog, _ = _catalog()
    assert catalog.is_known("nervos.test") is False
    assert catalog.is_configured("nervos.test") is False
