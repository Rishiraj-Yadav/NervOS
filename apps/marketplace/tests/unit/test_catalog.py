import pytest
from nervos_marketplace_service.application.catalog_queries import MarketplaceCatalogQueryService
from nervos_marketplace_service.domain.catalog import DistributionState, PublicationState, identity
from nervos_marketplace_service.domain.errors import MarketplaceError

from .test_public_api import FakeRepository


def test_states() -> None:
    assert list(PublicationState) == ["ready", "published"]
    assert list(DistributionState) == ["available", "yanked", "revoked"]


@pytest.mark.parametrize(
    "package,version", [("nervos.chat", "1.0.0"), ("../", "1.0.0"), ("com.acme.invoice", "latest")]
)
def test_identity_invalid(package: str, version: str) -> None:
    with pytest.raises(MarketplaceError) as error:
        identity(package, version)
    assert error.value.status == 422


@pytest.mark.parametrize("limit,query", [(0, ""), (101, ""), (1, "é" * 129)])
def test_query_bounds(limit: int, query: str) -> None:
    service = MarketplaceCatalogQueryService(FakeRepository())
    with pytest.raises(MarketplaceError):
        service.packages(query, limit, None)


def test_query_strip_and_exact_identity() -> None:
    service = MarketplaceCatalogQueryService(FakeRepository())
    assert len(service.packages("  invoice  ", 20, None)) == 1
    assert service.release("com.acme.invoice", "1.2.3").exact_version == "1.2.3"
    with pytest.raises(MarketplaceError):
        service.release("com.acme.invoice", "1.2.3+unknown")
