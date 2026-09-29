"""Integration tests for the /api/v1/packages API route group."""

# pyright: basic

from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "packages" / "nervos-core" / "tests" / "unit"))

import pytest
from fastapi.testclient import TestClient
from nervos_core.application.package_builder import PackageBuildInputs, package_build_bytes
from nervos_core.application.package_signing import Ed25519PackageSigner
from nervos_core.application.package_verification import verify_package
from package_fixtures import (  # pyright: ignore[reportMissingImports]
    TEST_SIGNING_SEED,
    VALID_CONFIG_SCHEMA,
    VALID_MANIFEST,
    valid_wheel,
)

ORIGIN = {"Origin": "http://localhost:5173"}


def _signer() -> Ed25519PackageSigner:
    return Ed25519PackageSigner.from_private_bytes(TEST_SIGNING_SEED)


def _built_bytes(manifest: bytes = VALID_MANIFEST) -> bytes:
    return package_build_bytes(
        PackageBuildInputs(
            manifest_bytes=manifest,
            config_schema_bytes=VALID_CONFIG_SCHEMA,
            agent_wheel_bytes=valid_wheel(),
        ),
        signer=_signer(),
    )


class FakeHealthChecker:
    def check(self, *, environment: Path, entrypoint: str, expected_sdk_api_version: str) -> None:
        pass


class FakeEnvBuilder:
    def __init__(self, store_root: Path, runtime: object) -> None:
        self._root = store_root
        self.runtime = runtime

    def build(self, verified: object, payload_root: Path):
        from nervos_core.application.package_environment import EnvironmentIdentity

        digest = getattr(verified, "content_digest", "env_sha")
        key = f"environments/{digest}"
        dest = self._root / key
        dest.mkdir(parents=True, exist_ok=True)
        return EnvironmentIdentity("{}", digest, key), dest


@pytest.fixture
def api_client(migrated_app, tmp_path: Path) -> TestClient:
    app, _ = migrated_app
    # Mock environment builder and health checker on app.state
    import hashlib

    from nervos_core.application.package_environment import (
        PackageRuntimeArtifacts,
        RuntimeWheelArtifact,
    )

    sdk_whl = tmp_path / "nervos_sdk-0.1.0-py3-none-any.whl"
    sdk_whl.write_bytes(b"sdk")
    sdk_sha = hashlib.sha256(b"sdk").hexdigest()
    host_whl = tmp_path / "nervos_package_host-0.1.0-py3-none-any.whl"
    host_whl.write_bytes(b"host")
    host_sha = hashlib.sha256(b"host").hexdigest()

    runtime = PackageRuntimeArtifacts(
        sdk=RuntimeWheelArtifact(sdk_whl, "nervos-sdk", "0.1.0", sdk_sha),
        host=RuntimeWheelArtifact(host_whl, "nervos-package-host", "0.1.0", host_sha),
    )
    store = app.state.package_application_service._store
    app.state.package_application_service._environment_builder = FakeEnvBuilder(
        store.environments_root, runtime
    )
    app.state.package_application_service._health_checker = FakeHealthChecker()

    client = TestClient(app)
    client.post(
        "/api/v1/setup",
        json={"username": "owner", "password": "A_valid_password_123"},
        headers=ORIGIN,
    )
    return client


def test_inspect_package_route(api_client: TestClient) -> None:
    raw = _built_bytes()
    response = api_client.post(
        "/api/v1/packages/inspect",
        files={"file": ("agent.nervos", io.BytesIO(raw), "application/octet-stream")},
        headers=ORIGIN,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["package_id"] == "com.acme.invoice"
    assert data["package_version"] == "1.2.3"
    assert data["is_compatible"] is True
    assert data["entrypoint_module"] == "acme_invoice.agent"
    assert data["entrypoint_object"] == "InvoiceAgent"


def test_install_and_query_package_routes(api_client: TestClient) -> None:
    raw = _built_bytes()
    ver = verify_package(archive_bytes=raw)

    # Install package
    install_resp = api_client.post(
        "/api/v1/packages/install",
        files={"file": ("agent.nervos", io.BytesIO(raw), "application/octet-stream")},
        data={
            "package_id": ver.manifest.package_id,
            "package_version": ver.manifest.package_version,
            "content_digest": ver.content_digest,
            "signer_fingerprint": ver.signer_fingerprint,
            "archive_digest": ver.archive_digest,
        },
        headers=ORIGIN,
    )
    assert install_resp.status_code == 201, install_resp.text
    summary = install_resp.json()
    assert summary["package_id"] == "com.acme.invoice"
    assert summary["status"] == "active"

    # List packages
    list_resp = api_client.get("/api/v1/packages", headers=ORIGIN)
    assert list_resp.status_code == 200
    items = list_resp.json()["items"]
    assert len(items) == 1
    assert items[0]["package_id"] == "com.acme.invoice"

    # Get package detail
    detail_resp = api_client.get("/api/v1/packages/com.acme.invoice/versions/1.2.3", headers=ORIGIN)
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["package_id"] == "com.acme.invoice"
    assert detail["display_name"] == "Acme Invoice Agent"
    assert "properties" in detail["config_schema"]

    # Get removal plan
    plan_resp = api_client.get(
        "/api/v1/packages/com.acme.invoice/versions/1.2.3/removal-plan", headers=ORIGIN
    )
    assert plan_resp.status_code == 200
    plan = plan_resp.json()
    assert plan["can_remove_immediately"] is True
    assert plan["bound_instances_count"] == 0

    # Delete package
    del_resp = api_client.delete("/api/v1/packages/com.acme.invoice/versions/1.2.3", headers=ORIGIN)
    assert del_resp.status_code == 200
    assert del_resp.json()["outcome"] == "removed"
