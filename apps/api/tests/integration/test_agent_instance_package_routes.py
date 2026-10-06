"""Integration tests for package-backed AgentInstance creation, config patch, and rebind API routes."""

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
SCHEMA_WITH_IMMUTABLE = b"""{
  "type": "object",
  "properties": {
    "account_id": {"type": "string", "x-nervos-immutable": true},
    "batch_size": {"type": "integer", "default": 10, "x-nervos-immutable": false}
  },
  "required": ["account_id"],
  "additionalProperties": false
}
"""


def _signer() -> Ed25519PackageSigner:
    return Ed25519PackageSigner.from_private_bytes(TEST_SIGNING_SEED)


def _built_bytes(manifest: bytes = VALID_MANIFEST, schema: bytes = VALID_CONFIG_SCHEMA) -> bytes:
    return package_build_bytes(
        PackageBuildInputs(
            manifest_bytes=manifest,
            config_schema_bytes=schema,
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
    app.state.package_application_service._installation_preflight = lambda: None

    client = TestClient(app)
    client.post(
        "/api/v1/setup",
        json={"username": "owner", "password": "A_valid_password_123"},
        headers=ORIGIN,
    )
    return client


def test_create_and_patch_package_agent_instance(api_client: TestClient) -> None:
    raw = _built_bytes(schema=SCHEMA_WITH_IMMUTABLE)
    ver = verify_package(archive_bytes=raw)

    api_client.post(
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

    # Create package-backed AgentInstance
    resp = api_client.post(
        "/api/v1/agent-instances",
        json={
            "agent_key": "com.acme.invoice",
            "agent_definition_version": "1.2.3",
            "display_name": "Invoice Agent",
            "model_provider": "anthropic",
            "model_name": "claude-3-5-sonnet",
            "package_config": {"account_id": "ACC100", "batch_size": 25},
        },
        headers=ORIGIN,
    )
    assert resp.status_code == 201, resp.text
    instance = resp.json()
    instance_id = instance["id"]
    assert instance["agent_key"] == "com.acme.invoice"

    # Patch config (mutable field)
    patch_resp = api_client.patch(
        f"/api/v1/agent-instances/{instance_id}/config",
        json={"config": {"account_id": "ACC100", "batch_size": 50}, "expected_config_revision": 1},
        headers=ORIGIN,
    )
    assert patch_resp.status_code == 200, patch_resp.text

    # Stale revision returns 409
    stale_resp = api_client.patch(
        f"/api/v1/agent-instances/{instance_id}/config",
        json={"config": {"account_id": "ACC100", "batch_size": 75}, "expected_config_revision": 1},
        headers=ORIGIN,
    )
    assert stale_resp.status_code == 409
    assert stale_resp.json()["error"]["code"] == "stale_config_revision"

    # Immutable field violation returns 422
    imm_resp = api_client.patch(
        f"/api/v1/agent-instances/{instance_id}/config",
        json={"config": {"account_id": "ACC999", "batch_size": 50}, "expected_config_revision": 2},
        headers=ORIGIN,
    )
    assert imm_resp.status_code == 422
    assert imm_resp.json()["error"]["code"] == "immutable_configuration_modified"


def test_rebind_and_rollback_package_agent_instance(api_client: TestClient) -> None:
    v1_raw = _built_bytes()
    v2_raw = _built_bytes(
        manifest=VALID_MANIFEST.replace(b"package_version: 1.2.3", b"package_version: 2.0.0")
    )

    ver1 = verify_package(archive_bytes=v1_raw)
    ver2 = verify_package(archive_bytes=v2_raw)

    api_client.post(
        "/api/v1/packages/install",
        files={"file": ("v1.nervos", io.BytesIO(v1_raw), "application/octet-stream")},
        data={
            "package_id": ver1.manifest.package_id,
            "package_version": ver1.manifest.package_version,
            "content_digest": ver1.content_digest,
            "signer_fingerprint": ver1.signer_fingerprint,
        },
        headers=ORIGIN,
    )
    api_client.post(
        "/api/v1/packages/install",
        files={"file": ("v2.nervos", io.BytesIO(v2_raw), "application/octet-stream")},
        data={
            "package_id": ver2.manifest.package_id,
            "package_version": ver2.manifest.package_version,
            "content_digest": ver2.content_digest,
            "signer_fingerprint": ver2.signer_fingerprint,
        },
        headers=ORIGIN,
    )

    create_resp = api_client.post(
        "/api/v1/agent-instances",
        json={
            "agent_key": "com.acme.invoice",
            "agent_definition_version": "1.2.3",
            "display_name": "Invoice Agent",
            "model_provider": "anthropic",
            "model_name": "claude-3-5-sonnet",
            "package_config": {"greeting": "v1"},
        },
        headers=ORIGIN,
    )
    instance_id = create_resp.json()["id"]

    # Rebind to v2
    rebind_resp = api_client.post(
        f"/api/v1/agent-instances/{instance_id}/rebind",
        json={"target_package_version": "2.0.0", "expected_config_revision": 1},
        headers=ORIGIN,
    )
    assert rebind_resp.status_code == 200
    assert rebind_resp.json()["agent_definition_version"] == "2.0.0"

    # Rollback to v1
    rollback_resp = api_client.post(
        f"/api/v1/agent-instances/{instance_id}/rebind",
        json={"target_package_version": "1.2.3", "expected_config_revision": 2},
        headers=ORIGIN,
    )
    assert rollback_resp.status_code == 200
    assert rollback_resp.json()["agent_definition_version"] == "1.2.3"


def test_builtin_chat_instance_creation_unaffected(api_client: TestClient) -> None:
    resp = api_client.post(
        "/api/v1/agent-instances",
        json={
            "agent_key": "nervos.chat",
            "agent_definition_version": "1",
            "display_name": "Built-in Chat",
            "model_provider": "anthropic",
            "model_name": "claude-3-5-sonnet",
        },
        headers=ORIGIN,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["agent_key"] == "nervos.chat"
