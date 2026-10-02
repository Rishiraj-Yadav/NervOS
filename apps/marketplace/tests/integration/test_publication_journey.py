"""Real hosted HTTP publication, isolated parser, PostgreSQL and version-pinned S3."""

# pyright: basic

import base64
from datetime import timedelta
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from nervos_marketplace_service.app import create_app
from nervos_marketplace_service.domain.identity import now, token_hash
from pydantic import SecretStr
from sqlalchemy import text

from ..fixtures import signed_package
from .local_runtime_journey import local_runtime_journey
from .test_acceptance import hosted_settings as hosted_settings_fixture

hosted_settings = hosted_settings_fixture

pytestmark = pytest.mark.marketplace_integration


def test_real_publication_and_exact_distribution(database, hosted_settings, monkeypatch, tmp_path):
    settings = hosted_settings.model_copy(
        update={
            "publisher_enabled": True,
            "public_origin": "https://marketplace.example",
            "writer_database_dsn": SecretStr(database.url.render_as_string(hide_password=False)),
            "oidc_issuer": "https://identity.example",
            "oidc_client_id": "fixture",
            "auth_transaction_encryption_key": SecretStr(base64.b64encode(b"x" * 32).decode()),
            "quarantine_access_key_id": SecretStr("fixture-quarantine-writer"),
            "quarantine_secret_access_key": SecretStr("synthetic-quarantine-writer-password"),
            "finalizer_access_key_id": SecretStr("fixture-finalizer-probe"),
            "finalizer_secret_access_key": SecretStr("synthetic-finalizer-probe-password"),
            "accepted_acr_values": ("mfa",),
            "required_amr_values": ("otp",),
        }
    )
    monkeypatch.setattr("nervos_marketplace_service.app.MarketplaceSettings", lambda: settings)
    account, token = uuid4(), "a" * 43
    at = now()
    with database.begin() as connection:
        connection.execute(
            text("INSERT INTO marketplace_accounts(id,state,created_at) VALUES(:id,'active',:at)"),
            {"id": account, "at": at},
        )
        connection.execute(
            text(
                "INSERT INTO cli_credentials(token_hash,account_id,authenticated_at,acr,amr,scope,"
                "created_at,expires_at,revoked) VALUES(:token,:id,:at,'mfa','[\"otp\"]',"
                "'publisher',:at,:expires,false)"
            ),
            {
                "id": account,
                "token": token_hash(token),
                "at": at,
                "expires": at + timedelta(minutes=15),
            },
        )
    headers = {"Authorization": f"Bearer {token}"}

    def runtime_fixture(version):
        source = (
            "from nervos_sdk import AgentResult\n"
            "class InvoiceAgent:\n"
            "    async def run(self, context):\n"
            f"        return AgentResult(final_message='stage-i-runtime:{version}')\n"
        ).encode()
        return signed_package("com.acme.journey", version, source=source)

    fixture = runtime_fixture("1.0.0")
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    with TestClient(create_app()) as client:

        def post(path, payload):
            response = client.post("/marketplace/v1" + path, json=payload, headers=headers)
            assert response.status_code == 200, response.text
            return response.json()

        publisher = post(
            "/publishers",
            {
                "handle": "journey",
                "display_name": "Journey",
                "kind": "individual",
            },
        )
        project = post(
            "/projects",
            {
                "publisher_id": publisher["id"],
                "package_id": "com.acme.journey",
            },
        )
        challenge = post(
            f"/publishers/{publisher['id']}/keys/challenge",
            {
                "public_key": base64.b64encode(key.public_key().public_bytes_raw()).decode(),
            },
        )
        registered = post(
            f"/publishers/{publisher['id']}/keys/prove",
            {
                "challenge_id": challenge["id"],
                "signature": base64.b64encode(
                    key.sign(base64.b64decode(challenge["payload"]))
                ).decode(),
            },
        )
        post(
            f"/projects/{project['id']}/keys",
            {
                "key_id": registered["id"],
                "ownership_revision": 1,
            },
        )
        operation = post(
            "/uploads",
            {
                "project_id": project["id"],
                "archive_sha256": fixture.verified.archive_digest,
                "size_bytes": len(fixture.raw),
                "ownership_revision": 1,
                "idempotency_key": "journey-first-upload",
            },
        )
        path = f"/uploads/{operation['id']}"
        uploaded = client.put(
            "/marketplace/v1" + path + "/artifact", content=fixture.raw, headers=headers
        )
        assert uploaded.status_code == 200, uploaded.text
        verified = post(path + "/verify", {})
        assert verified["state"] == "verified"
        # Verification is not publication: the public catalog still hides this release.
        assert client.get("/marketplace/v1/packages").json()["items"] == []
        post(
            f"/projects/{project['id']}/publish",
            {
                "exact_version": "1.0.0",
                "release_id": verified["ready_release_id"],
                "archive_sha256": fixture.verified.archive_digest,
                "content_digest": fixture.verified.content_digest,
                "ownership_revision": 1,
            },
        )
        assert (
            client.get("/marketplace/v1/packages").json()["items"][0]["package_id"]
            == "com.acme.journey"
        )
        artifact = client.get("/marketplace/v1/packages/com.acme.journey/versions/1.0.0/artifact")
        assert artifact.status_code == 200, artifact.text
        assert artifact.content == fixture.raw

        def publish_v2():
            second = runtime_fixture("2.0.0")
            operation = post(
                "/uploads",
                {
                    "project_id": project["id"],
                    "archive_sha256": second.verified.archive_digest,
                    "size_bytes": len(second.raw),
                    "ownership_revision": 1,
                    "idempotency_key": "journey-second-upload",
                },
            )
            path = f"/uploads/{operation['id']}"
            response = client.put(
                "/marketplace/v1" + path + "/artifact", content=second.raw, headers=headers
            )
            assert response.status_code == 200, response.text
            verified = post(path + "/verify", {})
            post(
                f"/projects/{project['id']}/publish",
                {
                    "exact_version": "2.0.0",
                    "release_id": verified["ready_release_id"],
                    "archive_sha256": second.verified.archive_digest,
                    "content_digest": second.verified.content_digest,
                    "ownership_revision": 1,
                },
            )

        def change_distribution(version, state):
            post(
                f"/projects/{project['id']}/distribution",
                {
                    "exact_version": version,
                    "state": state,
                    "status_revision": 1,
                    "reason": "Synthetic acceptance status change",
                },
            )

        local_runtime_journey(client.app, tmp_path, monkeypatch, publish_v2, change_distribution)
        post(
            f"/projects/{project['id']}/distribution",
            {
                "exact_version": "1.0.0",
                "state": "revoked",
                "status_revision": 2,
                "reason": "Synthetic acceptance revocation",
            },
        )
        assert (
            client.get(
                "/marketplace/v1/packages/com.acme.journey/versions/1.0.0/artifact"
            ).status_code
            == 410
        )
