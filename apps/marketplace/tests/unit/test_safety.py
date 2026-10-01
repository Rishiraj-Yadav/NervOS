"""Cleanup, bounded failures and public secret-marker regressions."""

import base64
import io
import json
import logging
import shutil
from pathlib import Path

import pytest
from botocore.stub import Stubber
from fastapi.testclient import TestClient
from nervos_marketplace_service.api.cursors import Cursor, decode, encode, fingerprint
from nervos_marketplace_service.api.router import ArtifactResponse
from nervos_marketplace_service.app import create_test_app
from nervos_marketplace_service.application.artifact_reads import ArtifactReadService
from nervos_marketplace_service.application.catalog_queries import MarketplaceCatalogQueryService
from nervos_marketplace_service.application.ports import CatalogPosition
from nervos_marketplace_service.config import MarketplaceSettings
from nervos_marketplace_service.domain.catalog import DistributionState, PackageDetail
from nervos_marketplace_service.domain.errors import MarketplaceError
from nervos_marketplace_service.infrastructure.s3_artifact_store import S3ArtifactStore, object_key
from starlette.requests import ClientDisconnect
from starlette.types import Message, Scope

from .test_artifact_reads import MemoryBody
from .test_public_api import BASE, FakeRepository, MemoryStore, MemoryStream, Ready


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.parametrize("state", [DistributionState.YANKED, DistributionState.REVOKED])
def test_final_status_recheck_closes_stream(state: DistributionState) -> None:
    repository = FakeRepository()

    class ChangingStore(MemoryStore):
        def open_verified(self, archive_sha256: str, expected_size: int) -> MemoryStream:
            stream = super().open_verified(archive_sha256, expected_size)
            repository.detail = repository.detail.model_copy(update={"distribution_state": state})
            return stream

    store = ChangingStore(repository.fixture.raw)
    service = ArtifactReadService(MarketplaceCatalogQueryService(repository), store)
    with pytest.raises(MarketplaceError) as error:
        service.open(repository.detail.package_id, "1.2.3", None)
    assert error.value.status == (409 if state == DistributionState.YANKED else 410)
    assert store.opened is not None and store.opened.closed


@pytest.mark.anyio
async def test_http_disconnect_closes_stream() -> None:
    stream = MemoryStream(b"bytes")
    response = ArtifactResponse(stream)

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        del message
        raise OSError("Disconnected")

    scope: Scope = {"type": "http", "asgi": {"spec_version": "2.4"}}
    with pytest.raises(ClientDisconnect):
        await response(scope, receive, send)
    assert stream.closed


def test_safe_unexpected_error_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    marker = "synthetic-private-marker"

    class BrokenRepository(FakeRepository):
        def package(self, package_id: str) -> PackageDetail:
            raise RuntimeError(marker + package_id)

    repository = BrokenRepository()
    with (
        caplog.at_level(logging.INFO, logger="nervos.marketplace"),
        TestClient(
            create_test_app(repository, MemoryStore(repository.fixture.raw), Ready()),
            raise_server_exceptions=False,
        ) as client,
    ):
        response = client.get(BASE, headers={"Authorization": "Bearer " + marker})
        # release() is available; exercise package() to produce the unexpected failure.
        response = client.get("/marketplace/v1/packages/com.acme.invoice?q=" + marker)
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "internal_error"
        assert response.headers["x-request-id"] == response.json()["error"]["request_id"]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert marker not in response.text + str(response.headers) + caplog.text


@pytest.mark.parametrize("change", ["filter", "route", "unknown", "version", "type"])
def test_strict_cursor_bindings(change: str) -> None:
    binding = fingerprint("versions", "com.acme.invoice", False, True)
    position = CatalogPosition(package_id="com.acme.invoice", version="1.2.3")
    raw = encode(position, "versions", binding)
    if change in {"unknown", "version", "type"}:
        value = Cursor(
            kind="versions",
            binding=binding,
            exact=0,
            rank=0,
            package_id=position.package_id,
            version=position.version,
        ).model_dump()
        if change == "unknown":
            value["unknown"] = "synthetic-private-marker"
        elif change == "version":
            value["v"] = 2
        else:
            value["rank"] = "1 OR 1=1"
        raw = base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
    with pytest.raises(MarketplaceError) as error:
        decode(
            raw,
            "packages" if change == "route" else "versions",
            fingerprint("versions", "com.acme.invoice", True, True)
            if change == "filter"
            else binding,
        )
    assert error.value.code == "invalid_request"


@pytest.mark.parametrize("hostile", ["../", "\\..", "' OR 1=1;--", "com.acme.invoice/.."])
def test_identity_injection_never_opens_storage(hostile: str) -> None:
    from urllib.parse import quote

    repository = FakeRepository()
    store = MemoryStore(repository.fixture.raw)
    with TestClient(create_test_app(repository, store, Ready())) as client:
        for path in (
            "/marketplace/v1/packages/" + quote(hostile, safe="") + "/versions/1.2.3/artifact",
            BASE.replace("1.2.3", quote(hostile, safe="")) + "/artifact",
        ):
            response = client.get(path)
            assert response.status_code in {404, 422}
    assert store.opened is None


def test_startup_marker_is_redacted(caplog: pytest.LogCaptureFixture) -> None:
    from unittest.mock import patch

    import nervos_marketplace_service.app as composition

    marker = "synthetic-private-startup-marker"
    with (
        patch.object(composition, "MarketplaceSettings", side_effect=ValueError(marker)),
        pytest.raises(RuntimeError) as error,
    ):
        composition.create_app()
    assert marker not in str(error.value) + repr(error.value) + caplog.text


def test_unusable_staging_health(
    settings: MarketplaceSettings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = S3ArtifactStore(settings)
    marker = "synthetic-private-path-marker"

    def inaccessible(_: Path):
        raise OSError(marker)

    monkeypatch.setattr(shutil, "disk_usage", inaccessible)
    repository = FakeRepository()
    with (
        Stubber(store.client) as stub,
        TestClient(create_test_app(repository, store, store)) as client,
    ):
        stub.add_response("head_bucket", {}, {"Bucket": settings.s3_bucket})
        assert client.get("/health/live").status_code == 200
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert marker not in response.text + str(response.headers) + caplog.text
    store.close()


def test_storage_capacity_and_readiness(
    settings: MarketplaceSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = S3ArtifactStore(settings)
    usage = shutil.disk_usage(settings.artifact_temp_directory)

    def no_space(_: Path):
        return usage._replace(free=0)

    monkeypatch.setattr(shutil, "disk_usage", no_space)
    with Stubber(store.client) as stub:
        stub.add_response("head_bucket", {}, {"Bucket": settings.s3_bucket})
        assert not store.ready()
    with pytest.raises(MarketplaceError):
        store.open_verified("a" * 64, 1)
    assert store.admission.acquire(False)
    store.admission.release()
    store.close()


@pytest.mark.parametrize("failure", ["timeout", "read", "declared", "deadline"])
def test_body_failure_cleanup(
    settings: MarketplaceSettings, tmp_path: Path, failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nervos_marketplace_service.infrastructure.s3_artifact_store as storage_module

    from ..fixtures import signed_package

    class Clock:
        calls = 0

        def monotonic(self) -> float:
            self.calls += 1
            return 0.0 if self.calls == 1 else 1000.0

    if failure == "deadline":
        monkeypatch.setattr(storage_module, "time", Clock())

    fixture = signed_package()
    store = S3ArtifactStore(settings)

    class FailingBody(MemoryBody):
        def set_socket_timeout(self, timeout: float) -> None:
            if failure == "timeout":
                raise TimeoutError("synthetic-private-marker")
            super().set_socket_timeout(timeout)

        def read(self, amt: int | None = None) -> bytes:
            if failure == "read":
                raise OSError("synthetic-private-marker")
            return super().read(amt)

    params = {"Bucket": settings.s3_bucket, "Key": object_key(fixture.verified.archive_digest)}
    with Stubber(store.client) as stub:
        stub.add_response("head_object", {"ContentLength": len(fixture.raw)}, params)
        raw = io.BytesIO(fixture.raw)
        body = FailingBody(raw, len(fixture.raw))
        stub.add_response(
            "get_object",
            {"ContentLength": 1 if failure == "declared" else len(fixture.raw), "Body": body},
            params,
        )
        with pytest.raises(MarketplaceError) as error:
            store.open_verified(fixture.verified.archive_digest, len(fixture.raw))
        assert str(error.value) == "artifact_unavailable"
        assert raw.closed
    assert not list(tmp_path.iterdir())
    assert store.admission.acquire(False)
    store.admission.release()
    store.close()
