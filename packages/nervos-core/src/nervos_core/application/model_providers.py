"""Exact canonical model-provider resolution with explicit unavailable state."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from nervos_core.application.model_completion import ModelCompletion


class UnknownModelProvider(LookupError):
    """Raised when no provider is known for the canonical provider identifier."""


class ModelProviderUnavailable(LookupError):
    """Raised when a known provider is not configured for this process."""


class DuplicateModelProvider(ValueError):
    """Raised when a canonical provider identifier is registered twice."""


class ModelProviderCatalog:
    """Immutable exact-identifier provider catalog.

    A provider identifier is *known* once it appears in the catalog, independently of
    whether this process holds the configuration required to build its adapter. A known
    identifier whose configuration is absent resolves to `ModelProviderUnavailable`, which
    is a pre-run rejection rather than an unknown provider.
    """

    def __init__(
        self,
        entries: Iterable[tuple[str, Callable[[], ModelCompletion]]],
        *,
        known: Iterable[str] = (),
    ) -> None:
        configured: dict[str, Callable[[], ModelCompletion]] = {}
        for provider_id, factory in entries:
            if provider_id in configured:
                raise DuplicateModelProvider(provider_id)
            configured[provider_id] = factory
        self._configured = configured
        # A registered provider is always a known identifier.
        self._known = frozenset(set(known) | set(configured))

    def is_known(self, provider_id: str) -> bool:
        """Return whether the canonical identifier is a provider NervOS understands."""
        return provider_id in self._known

    def is_configured(self, provider_id: str) -> bool:
        """Return whether this process can build a completion for the identifier."""
        return provider_id in self._configured

    def resolve(self, provider_id: str) -> ModelCompletion:
        """Return the configured completion or raise a safe pre-run provider error."""
        factory = self._configured.get(provider_id)
        if factory is None:
            if provider_id in self._known:
                raise ModelProviderUnavailable(provider_id)
            raise UnknownModelProvider(provider_id)
        return factory()
