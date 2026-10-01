import base64
import hashlib
import io
import sys
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from nervos_marketplace_service.api.cursors import decode, encode, fingerprint
from nervos_marketplace_service.app import create_test_app
from nervos_marketplace_service.application.ports import ArtifactStat, CatalogPosition
from nervos_marketplace_service.domain.catalog import (
    DistributionState,
    PackageDetail,
    PackageReleaseDetail,
    PackageSummary,
    PackageVersionSummary,
)
from nervos_marketplace_service.domain.errors import MarketplaceError

from ..fixtures import NOW, descriptor, signed_package


class FakeRepository:
    def __init__(self) -> None:
        self.fixture = signed_package()
        self.detail = descriptor(self.fixture)
        self.down = False

    def packages(
        self, query: str, limit: int, after: CatalogPosition | None
    ) -> list[tuple[PackageSummary, CatalogPosition]]:
        if self.down:
            raise MarketplaceError("service_unavailable", 503)
        if after or query == "absent":
            return []
        value = self.package(self.detail.package_id)
        return [
            (
                PackageSummary.model_validate(
                    {k: getattr(value, k) for k in PackageSummary.model_fields}
                ),
                CatalogPosition(package_id=value.package_id),
            )
        ]

    def package(self, package_id: str) -> PackageDetail:
        self.release(package_id, self.detail.exact_version)
        return PackageDetail(
            package_id=package_id,
            display_name="Invoice",
            summary="Synthetic",
            listing_revision=1,
            latest_stable_version="1.2.3",
            description="Plain text",
            listing_updated_at=NOW,
            has_available_release=True,
        )

    def versions(
        self,
        package_id: str,
        limit: int,
        after: CatalogPosition | None,
        include_unavailable: bool,
        include_prerelease: bool,
    ) -> list[tuple[PackageVersionSummary, CatalogPosition]]:
        self.package(package_id)
        return [
            (
                PackageVersionSummary.model_validate(
                    {k: getattr(self.detail, k) for k in PackageVersionSummary.model_fields}
                ),
                CatalogPosition(package_id=package_id, version=self.detail.exact_version),
            )
        ]

    def release(self, package_id: str, version: str) -> PackageReleaseDetail:
        if package_id != self.detail.package_id or version != self.detail.exact_version:
            raise MarketplaceError("not_found", 404)
        return self.detail


class MemoryStream:
    def __init__(self, data: bytes) -> None:
        self.file = io.BytesIO(data)
        self.size_bytes = len(data)
        self.archive_sha256 = hashlib.sha256(data).hexdigest()
        self.closed = False

    def chunks(self) -> Iterator[bytes]:
        yield self.file.read()

    def close(self) -> None:
        self.file.close()
        self.closed = True


class MemoryStore:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.opened: MemoryStream | None = None
        self.fail = False

    def stat(self, archive_sha256: str) -> ArtifactStat:
        return ArtifactStat(len(self.data))

    def open_verified(self, archive_sha256: str, expected_size: int) -> MemoryStream:
        if self.fail:
            raise MarketplaceError("artifact_unavailable", 503)
        self.opened = MemoryStream(self.data)
        return self.opened

    def close(self) -> None:
        if self.opened:
            self.opened.close()


class Ready:
    def ready(self) -> bool:
        return True


@pytest.fixture
def repository() -> FakeRepository:
    return FakeRepository()


@pytest.fixture
def client(repository: FakeRepository) -> Iterator[TestClient]:
    with TestClient(
        create_test_app(repository, MemoryStore(repository.fixture.raw), Ready()),
        raise_server_exceptions=False,
    ) as client:
        yield client


BASE = "/marketplace/v1/packages/com.acme.invoice/versions/1.2.3"


@pytest.mark.parametrize(
    "path,status",
    [
        ("/health/live", 200),
        ("/health/ready", 200),
        ("/marketplace/v1/packages", 200),
        ("/marketplace/v1/packages?q=absent", 200),
        ("/marketplace/v1/packages/com.acme.invoice", 200),
        ("/marketplace/v1/packages/com.acme.invoice/versions", 200),
        (BASE, 200),
        (BASE.replace("1.2.3", "2.0.0"), 404),
        ("/marketplace/v1/packages/com.missing.test", 404),
        ("/marketplace/v1/packages/nervos.chat", 422),
        (BASE.replace("1.2.3", "latest"), 422),
        ("/marketplace/v1/packages?limit=0", 422),
        ("/marketplace/v1/packages?limit=101", 422),
        ("/marketplace/v1/packages?cursor=bad", 422),
        ("/marketplace/v1/packages?q=" + "é" * 129, 422),
    ],
)
def test_routes(client: TestClient, path: str, status: int) -> None:
    response = client.get(path)
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert len(response.headers["x-request-id"]) == 32
    if status >= 400:
        assert response.json()["error"]["request_id"] == response.headers["x-request-id"]


@pytest.mark.parametrize(
    "state,ack,status",
    [
        ("available", False, 200),
        ("yanked", False, 409),
        ("yanked", True, 200),
        ("revoked", True, 410),
    ],
)
def test_distribution(
    client: TestClient, repository: FakeRepository, state: str, ack: bool, status: int
) -> None:
    repository.detail = repository.detail.model_copy(
        update={"distribution_state": DistributionState(state)}
    )
    response = client.get(
        BASE + "/artifact",
        headers={
            "X-NervOS-Acknowledge-Yanked": repository.detail.archive_sha256 if ack else "wrong"
        },
    )
    assert response.status_code == status
    assert client.get(BASE).status_code == 200
    if status == 200:
        assert response.content == repository.fixture.raw
        digest = base64.b64encode(hashlib.sha256(response.content).digest()).decode()
        assert response.headers["content-digest"] == f"sha-256=:{digest}:"
        assert response.headers["content-length"] == str(len(response.content))
        assert response.headers["content-disposition"] == 'attachment; filename="artifact.nervos"'
        assert response.headers["cache-control"] == "no-store, no-transform"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "acme_invoice.agent" not in sys.modules


def test_range(client: TestClient) -> None:
    response = client.get(BASE + "/artifact", headers={"Range": "bytes=0-10"})
    assert response.status_code == 416
    assert response.json()["error"]["code"] == "range_not_supported"


def test_cursor_contract() -> None:
    binding = fingerprint("packages", "invoice")
    position = CatalogPosition(package_id="com.acme.invoice", rank=5)
    raw = encode(position, "packages", binding)
    assert decode(raw, "packages", binding) == position
    for malformed in (raw, "@", "a" * 2049, base64.urlsafe_b64encode(b'{"unknown":1}').decode()):
        with pytest.raises(MarketplaceError):
            decode(malformed, "packages", fingerprint("packages", "different"))


def test_safe_dependency_error(client: TestClient, repository: FakeRepository) -> None:
    repository.down = True
    response = client.get("/marketplace/v1/packages")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
