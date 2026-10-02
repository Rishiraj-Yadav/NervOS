"""Fail/pass qualification of pinned SeaweedFS version immutability, in disposable storage.

Conditional PUT provides creation concurrency; exact VersionId and deletion denial
provide byte immutability. No cloud/deployment credentials or developer data are used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import uuid4

import boto3
from alembic import command
from alembic.config import Config as AlembicConfig
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from nervos_marketplace_service.app import create_test_app
from nervos_marketplace_service.config import MarketplaceSettings
from nervos_marketplace_service.domain.artifact_reference import FinalizedArtifactRef
from nervos_marketplace_service.domain.errors import MarketplaceError
from nervos_marketplace_service.domain.semver_order import precedence_key
from nervos_marketplace_service.infrastructure.catalog_repository import PostgresCatalogRepository
from nervos_marketplace_service.infrastructure.s3_artifact_store import S3ArtifactStore
from nervos_marketplace_service.infrastructure.s3_final_artifacts import S3FinalArtifacts
from pydantic import SecretStr
from sqlalchemy import create_engine, insert, text
from sqlalchemy.exc import SQLAlchemyError

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

ROOT = Path(__file__).resolve().parents[1]
BUCKET = "marketplace-test"
IMAGE = (
    "chrislusf/seaweedfs:4.48@sha256:"
    "4e61d15fd35994cb1e43e1e553dff106794841fd9a99ade2fc8c8bfce4d7872d"
)


def version_id(value: str | None) -> str:
    if (
        value is None
        or value == "null"
        or re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,1024}", value) is None
    ):
        raise AssertionError("Storage did not return a usable VersionId")
    return value


def client(endpoint: str, access: str, secret: str) -> S3Client:
    return boto3.client(  # pyright: ignore[reportUnknownMemberType]
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        config=Config(
            connect_timeout=2,
            read_timeout=5,
            retries={"total_max_attempts": 1},
            proxies={},
            s3={"addressing_style": "path"},
        ),
    )


def body(client: S3Client, key: str, version: str | None = None) -> bytes:
    response = (
        client.get_object(Bucket=BUCKET, Key=key, VersionId=version)
        if version is not None
        else client.get_object(Bucket=BUCKET, Key=key)
    )
    stream = response["Body"]
    try:
        value = stream.read(4097)
        if len(value) > 4096 or len(value) != response["ContentLength"]:
            raise AssertionError("Unexpected probe body length")
        return value
    finally:
        stream.close()


def final_key(data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    return f"artifacts/sha256/{digest[:2]}/{digest}.nervos"


def protection_policy() -> str:
    # Deny all final object deletions, including versions/batch deletes. Normal
    # identities also lack bucket-scoped Write, Admin, Tagging and policy rights.
    # Maintenance may explicitly change this policy outside normal service routes.
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion"],
                    "Resource": f"arn:aws:s3:::{BUCKET}/artifacts/*",
                }
            ],
        }
    )


def qualify(
    admin: S3Client,
    writer: S3Client,
    reader: S3Client,
    quarantine: S3Client,
    evidence: dict[str, object],
) -> None:
    failures: list[str] = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    def denied(label: str, operation: Callable[[], object]) -> None:
        try:
            operation()
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
            evidence[label] = {"status": status, "code": code}
            require(
                status == 403 and code == "AccessDenied", label + " was not an authorization denial"
            )
        else:
            evidence[label] = "UNEXPECTED SUCCESS"
            failures.append(label + " unexpectedly succeeded")

    admin.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
    status = admin.get_bucket_versioning(Bucket=BUCKET).get("Status")
    evidence["versioning_status"] = status
    require(status == "Enabled", "Versioning was not enabled")
    require(
        writer.get_bucket_versioning(Bucket=BUCKET).get("Status") == "Enabled",
        "Finalizer cannot perform fail-closed versioning readiness",
    )
    admin.put_bucket_policy(Bucket=BUCKET, Policy=protection_policy())
    a, b = b"original:" + uuid4().bytes, b"replacement:" + uuid4().bytes
    key = final_key(a)
    first = writer.put_object(Bucket=BUCKET, Key=key, Body=a, IfNoneMatch="*")
    v1 = version_id(first.get("VersionId"))
    evidence["v1"] = v1
    require(body(reader, key, v1) == a, "Initial V1 read was incorrect")
    head = reader.head_object(Bucket=BUCKET, Key=key, VersionId=v1)
    require(head["ContentLength"] == len(a) and head.get("VersionId") == v1, "Pinned HEAD differed")
    try:
        writer.put_object(Bucket=BUCKET, Key=key, Body=a, IfNoneMatch="*")
    except ClientError as error:
        result = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        evidence["conditional_duplicate_status"] = result
        require(result == 412, "Duplicate conditional creation did not return 412")
    else:
        failures.append("Duplicate conditional creation succeeded")
    second = writer.put_object(Bucket=BUCKET, Key=key, Body=b)
    v2 = version_id(second.get("VersionId"))
    evidence["v2"] = v2
    require(v1 != v2, "An unconditional PUT did not create a distinct version")
    original = body(reader, key, v1)
    current = body(reader, key, v2)
    evidence["v1_after_v2_sha256"] = hashlib.sha256(original).hexdigest()
    evidence["v2_sha256"] = hashlib.sha256(current).hexdigest()
    evidence["v1_size"] = len(original)
    evidence["v2_size"] = len(current)
    require(original == a, "An unconditional PUT changed V1")
    require(current == b and body(reader, key) == b, "V2/current read was incorrect")
    # A storage-specific internal namespace must not offer an alternate way to
    # mutate the pinned leaf using otherwise authorized final-prefix PUT/Copy.
    # Exercise the real provider; application key construction is not the proof.
    reserved_key = key + ".versions/" + v1
    try:
        nested = writer.put_object(Bucket=BUCKET, Key=reserved_key, Body=b)
        evidence["internal_namespace_put"] = {
            "status": nested["ResponseMetadata"].get("HTTPStatusCode"),
            "version": nested.get("VersionId"),
        }
    except ClientError as error:
        evidence["internal_namespace_put"] = {
            "status": error.response.get("ResponseMetadata", {}).get("HTTPStatusCode"),
        }
    require(body(reader, key, v1) == a, "Internal-namespace PUT mutated pinned V1")
    try:
        nested_copy = writer.copy_object(
            Bucket=BUCKET,
            Key=reserved_key,
            CopySource={"Bucket": BUCKET, "Key": key, "VersionId": v2},
        )
        evidence["internal_namespace_copy"] = {
            "status": nested_copy["ResponseMetadata"].get("HTTPStatusCode"),
            "version": nested_copy.get("VersionId"),
        }
    except ClientError as error:
        evidence["internal_namespace_copy"] = {
            "status": error.response.get("ResponseMetadata", {}).get("HTTPStatusCode"),
        }
    require(body(reader, key, v1) == a, "Internal-namespace Copy mutated pinned V1")
    # Recovery must read and hash the exact version observed by HEAD; current V2
    # mismatches key's digest, so it cannot be adopted as the original Artifact.
    observed = version_id(writer.head_object(Bucket=BUCKET, Key=key).get("VersionId"))
    require(observed == v2, "Recovery HEAD did not identify current version")
    require(
        hashlib.sha256(body(writer, key, observed)).hexdigest() != hashlib.sha256(a).hexdigest(),
        "Wrong current bytes were not distinguishable by full digest",
    )
    evidence["wrong_current_adoption"] = "rejected by complete SHA-256 comparison"

    for name, identity in (("finalizer", writer), ("reader", reader), ("quarantine", quarantine)):
        denied(
            name + "_delete_version",
            lambda identity=identity: identity.delete_object(Bucket=BUCKET, Key=key, VersionId=v1),
        )
        denied(
            name + "_delete_current",
            lambda identity=identity: identity.delete_object(Bucket=BUCKET, Key=key),
        )
        # S3 batch deletion reports per-object errors inside an HTTP 200 response.
        # A completed request is not evidence that the requested version was deleted.
        batch = identity.delete_objects(
            Bucket=BUCKET, Delete={"Objects": [{"Key": key, "VersionId": v2}]}
        )
        errors = batch.get("Errors", [])
        evidence[name + "_batch_delete"] = {
            "status": batch["ResponseMetadata"].get("HTTPStatusCode"),
            "errors": errors,
            "deleted": batch.get("Deleted", []),
        }
        require(
            not batch.get("Deleted")
            and len(errors) == 1
            and errors[0].get("Code") == "AccessDenied"
            and errors[0].get("Key") == key,
            name + " batch deletion did not deny the target version",
        )
        denied(
            name + "_suspend_versioning",
            lambda identity=identity: identity.put_bucket_versioning(
                Bucket=BUCKET, VersioningConfiguration={"Status": "Suspended"}
            ),
        )
        denied(
            name + "_delete_bucket", lambda identity=identity: identity.delete_bucket(Bucket=BUCKET)
        )
        denied(
            name + "_put_policy",
            lambda identity=identity: identity.put_bucket_policy(
                Bucket=BUCKET, Policy=protection_policy()
            ),
        )
        denied(
            name + "_delete_policy",
            lambda identity=identity: identity.delete_bucket_policy(Bucket=BUCKET),
        )
        denied(
            name + "_put_lifecycle",
            lambda identity=identity: identity.put_bucket_lifecycle_configuration(
                Bucket=BUCKET,
                LifecycleConfiguration={
                    "Rules": [
                        {
                            "ID": "probe",
                            "Status": "Enabled",
                            "Filter": {"Prefix": "artifacts/"},
                            "Expiration": {"Days": 1},
                        }
                    ]
                },
            ),
        )
        denied(
            name + "_put_object_lock",
            lambda identity=identity: identity.put_object_lock_configuration(
                Bucket=BUCKET, ObjectLockConfiguration={"ObjectLockEnabled": "Enabled"}
            ),
        )
    denied("reader_write", lambda: reader.put_object(Bucket=BUCKET, Key=key, Body=b))
    denied("quarantine_final_write", lambda: quarantine.put_object(Bucket=BUCKET, Key=key, Body=b))
    quarantine.put_object(Bucket=BUCKET, Key="quarantine/" + uuid4().hex, Body=b"private")
    denied(
        "reader_quarantine_read",
        lambda: reader.get_object(Bucket=BUCKET, Key="quarantine/no-access"),
    )
    require(
        admin.get_bucket_versioning(Bucket=BUCKET).get("Status") == "Enabled",
        "Routine identities altered versioning",
    )
    try:
        require(body(reader, key, v1) == a, "V1 changed after denied deletions")
    except ClientError:
        failures.append("V1 was unavailable after routine deletion attempts")

    # Two simultaneous requests to an absent digest key must have one winner.
    data = b"concurrent:" + uuid4().bytes
    concurrent_key = final_key(data)
    barrier = Barrier(2)

    def create() -> tuple[int, str | None]:
        barrier.wait(timeout=10)
        try:
            result = writer.put_object(
                Bucket=BUCKET, Key=concurrent_key, Body=data, IfNoneMatch="*"
            )
            return 200, result.get("VersionId")
        except ClientError as error:
            return error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0), None

    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(create), pool.submit(create)]
        results = [task.result(timeout=20) for task in tasks]
    evidence["concurrent_statuses"] = sorted(item[0] for item in results)
    require(
        sorted(item[0] for item in results) == [200, 412],
        "Concurrent creation was not single-winner",
    )
    winner = next((item[1] for item in results if item[0] == 200), None)
    if winner:
        require(
            body(reader, concurrent_key, version_id(winner)) == data,
            "Concurrent winner bytes differed",
        )

    # Privileged diagnostics deliberately change default state, then restore policy.
    admin.delete_bucket_policy(Bucket=BUCKET)
    marker = admin.delete_object(Bucket=BUCKET, Key=concurrent_key)
    admin.put_bucket_policy(Bucket=BUCKET, Policy=protection_policy())
    evidence["delete_marker"] = marker.get("DeleteMarker", False)
    require(marker.get("DeleteMarker", False), "Admin did not create a delete marker")
    if winner:
        require(
            body(reader, concurrent_key, version_id(winner)) == data,
            "Delete marker made pinned version unreadable",
        )
    evidence["failures"] = failures
    if failures:
        raise AssertionError("; ".join(failures))


def qualify_application(
    endpoint: str, dsn: str, admin: S3Client, evidence: dict[str, object]
) -> None:
    """Real hosted migrations/Artifact evidence/HTTP read; no local runtime DB."""
    # Explicit source-tree test import; never part of production service imports.
    sys.path.insert(0, str(ROOT))
    from nervos_core.domain.packages import PackageVersion
    from nervos_marketplace_service.infrastructure.models import (
        ArtifactRow,
        PackageListingRow,
        PackageProjectRow,
        PackageReleaseRow,
    )

    from apps.marketplace.tests.fixtures import NOW, signed_package

    previous = os.environ.get("NERVOS_MARKETPLACE_DATABASE_DSN")
    os.environ["NERVOS_MARKETPLACE_DATABASE_DSN"] = dsn
    try:
        command.upgrade(AlembicConfig(str(ROOT / "apps/marketplace/alembic.ini")), "head")
    finally:
        if previous is None:
            os.environ.pop("NERVOS_MARKETPLACE_DATABASE_DSN", None)
        else:
            os.environ["NERVOS_MARKETPLACE_DATABASE_DSN"] = previous
    engine = create_engine(dsn, hide_parameters=True)
    try:
        with tempfile.TemporaryDirectory(prefix="mp-versioned-http-") as directory:
            settings = MarketplaceSettings(
                environment="test",
                database_dsn=SecretStr(dsn),
                s3_endpoint_url=endpoint,
                s3_region="us-east-1",
                s3_bucket=BUCKET,
                s3_access_key_id=SecretStr("fixture-finalizer-probe"),
                s3_secret_access_key=SecretStr("synthetic-finalizer-probe-password"),
                artifact_temp_directory=Path(directory),
            )
            finalizer = S3FinalArtifacts(settings)
            fixture = signed_package()
            path = Path(directory) / "source.nervos"
            path.write_bytes(fixture.raw)
            digest = fixture.verified.archive_digest
            reference = finalizer.create_final_if_absent(digest, path, len(fixture.raw))
            repeated = finalizer.create_final_if_absent(digest, path, len(fixture.raw))
            if reference != repeated:
                raise AssertionError("Production crash/retry adoption did not converge")
            evidence["production_finalization_adoption"] = "same fully verified VersionId"
            concurrent_fixture = signed_package(version="1.2.4")
            concurrent_path = Path(directory) / "concurrent.nervos"
            concurrent_path.write_bytes(concurrent_fixture.raw)
            barrier = Barrier(2)

            def finalize_concurrently() -> FinalizedArtifactRef:
                barrier.wait(timeout=10)
                return finalizer.create_final_if_absent(
                    concurrent_fixture.verified.archive_digest,
                    concurrent_path,
                    len(concurrent_fixture.raw),
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(finalize_concurrently) for _ in range(2)]
                references = [future.result(timeout=30) for future in futures]
            if references[0] != references[1]:
                raise AssertionError("Concurrent production finalizers did not converge")
            evidence["production_concurrent_versions"] = [ref.version_id for ref in references]
            publisher, project = uuid4(), uuid4()
            with engine.begin() as connection:
                for invalid in ("", "null", "v\nheader", "v\\id", " " + "v", "v" * 1025):
                    savepoint = connection.begin_nested()
                    try:
                        connection.execute(
                            insert(ArtifactRow).values(
                                archive_sha256=hashlib.sha256(uuid4().bytes).hexdigest(),
                                size_bytes=1,
                                created_at=NOW,
                                storage_version_id=invalid,
                            )
                        )
                    except SQLAlchemyError:
                        savepoint.rollback()
                    else:
                        savepoint.rollback()
                        raise AssertionError("Database accepted invalid version evidence")
                savepoint = connection.begin_nested()
                connection.execute(
                    insert(ArtifactRow).values(
                        archive_sha256=hashlib.sha256(uuid4().bytes).hexdigest(),
                        size_bytes=1,
                        created_at=NOW,
                        storage_version_id="v" * 1024,
                    )
                )
                savepoint.rollback()
            evidence["database_invalid_version_rejections"] = 6
            evidence["database_accepts_1024_character_version"] = True
            with engine.begin() as connection:
                connection.execute(
                    text("""INSERT INTO publishers
                    (id,handle,display_name,kind,state,revision,created_at,updated_at)
                    VALUES(:id,'storage-fixture','Storage Fixture','organization',
                           'active',1,:at,:at)"""),
                    {"id": publisher, "at": NOW},
                )
                connection.execute(
                    insert(PackageProjectRow).values(
                        id=project,
                        package_id=fixture.verified.manifest.package_id,
                        publisher_id=publisher,
                        created_at=NOW,
                    )
                )
                connection.execute(
                    insert(PackageListingRow).values(
                        project_id=project,
                        display_name="Storage Fixture",
                        summary="Synthetic",
                        description="Exact version read proof",
                        revision=1,
                        updated_at=NOW,
                    )
                )
                connection.execute(
                    insert(ArtifactRow).values(
                        archive_sha256=digest,
                        size_bytes=len(fixture.raw),
                        created_at=NOW,
                        storage_version_id=reference.version_id,
                    )
                )
                parsed = PackageVersion(fixture.verified.manifest.package_version)
                connection.execute(
                    insert(PackageReleaseRow).values(
                        id=uuid4(),
                        project_id=project,
                        original_publisher_id=publisher,
                        exact_version=parsed.value,
                        archive_sha256=digest,
                        content_digest=fixture.verified.content_digest,
                        signer_fingerprint=fixture.verified.signer_fingerprint,
                        manifest_version=1,
                        manifest_bytes=fixture.manifest,
                        runtime_language="python",
                        runtime_python=">=3.12,<4",
                        nervos_min_version="0.1.0",
                        nervos_max_version="0.1.0",
                        semver_key=precedence_key(parsed),
                        is_prerelease=bool(parsed.prerelease),
                        publication_state="published",
                        distribution_state="available",
                        status_revision=1,
                        published_at=NOW,
                        status_updated_at=NOW,
                    )
                )
            corrupt = b"X" + fixture.raw[1:]
            changed = finalizer.client.put_object(Bucket=BUCKET, Key=reference.key, Body=corrupt)
            if version_id(changed.get("VersionId")) == reference.version_id:
                raise AssertionError("Corrupt current version replaced pinned version")
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "UPDATE artifacts SET storage_version_id=:version "
                            "WHERE archive_sha256=:digest"
                        ),
                        {"version": changed["VersionId"], "digest": digest},
                    )
            except SQLAlchemyError:
                evidence["database_rejects_version_change"] = True
            else:
                raise AssertionError("Database permitted changing immutable version evidence")
            try:
                finalizer.create_final_if_absent(digest, path, len(fixture.raw))
            except MarketplaceError:
                evidence["production_wrong_current_adoption"] = "rejected"
            else:
                raise AssertionError("Production finalizer adopted corrupt current bytes")
            reader_settings = settings.model_copy(
                update={
                    "s3_access_key_id": SecretStr("fixture-version-reader"),
                    "s3_secret_access_key": SecretStr("synthetic-version-reader-password"),
                }
            )
            reader_store = S3ArtifactStore(reader_settings)
            try:
                app = create_test_app(PostgresCatalogRepository(engine), reader_store, reader_store)
                route = "/marketplace/v1/packages/com.acme.invoice/versions/1.2.3/artifact"
                with TestClient(app) as http:
                    response = http.get(route)
                    if response.status_code != 200 or response.content != fixture.raw:
                        raise AssertionError(
                            "HTTP endpoint did not serve DB-pinned V1 after corrupt V2"
                        )
                    evidence["http_pinned_v1_after_corrupt_v2"] = hashlib.sha256(
                        response.content
                    ).hexdigest()
                    descriptor = http.get(route.removesuffix("/artifact")).json()
                    if "storage_version_id" in descriptor or "version_id" in descriptor:
                        raise AssertionError("Storage version leaked into public DTO")
                    admin.delete_bucket_policy(Bucket=BUCKET)
                    admin.delete_object(Bucket=BUCKET, Key=reference.key)
                    admin.put_bucket_policy(Bucket=BUCKET, Policy=protection_policy())
                    response = http.get(route)
                    if response.status_code != 200 or response.content != fixture.raw:
                        raise AssertionError("HTTP endpoint lost DB-pinned V1 after delete marker")
                    evidence["http_pinned_v1_after_delete_marker"] = "exact original package"
            finally:
                reader_store.close()
                finalizer.close()
            with engine.connect() as connection:
                evidence["hosted_migration_head"] = connection.execute(
                    text("SELECT version_num FROM marketplace_alembic_version")
                ).scalar_one()
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-file", type=Path)
    args = parser.parse_args()
    docker = shutil.which("docker")
    if docker is None:
        raise SystemExit("Disposable versioning qualification requires Docker")
    prefix = [
        docker,
        "compose",
        "-f",
        str(ROOT / "apps/marketplace/compose.test.yml"),
        "-p",
        "nervos-versioning-probe-" + uuid4().hex[:12],
    ]
    environment = {**os.environ, "MP_TEST_PG_PORT": "0", "MP_TEST_S3_PORT": "0"}
    evidence: dict[str, object] = {"image": IMAGE, "passed": False}
    clients: list[S3Client] = []
    try:
        subprocess.run(
            [*prefix, "up", "-d", "--wait"], env=environment, cwd=ROOT, check=True, timeout=90
        )
        port = (
            subprocess.run(
                [*prefix, "port", "s3", "8333"],
                env=environment,
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            .stdout.strip()
            .rsplit(":", 1)[1]
        )
        endpoint = f"http://127.0.0.1:{port}"
        for access, secret in (
            ("fixture-admin", "synthetic-admin-password"),
            ("fixture-finalizer-probe", "synthetic-finalizer-probe-password"),
            ("fixture-version-reader", "synthetic-version-reader-password"),
            ("fixture-quarantine-writer", "synthetic-quarantine-writer-password"),
        ):
            clients.append(client(endpoint, access, secret))
        deadline = time.monotonic() + 60
        while True:
            try:
                clients[0].head_bucket(Bucket=BUCKET)
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise RuntimeError("Disposable S3 readiness failed") from None
                time.sleep(1)
        qualify(clients[0], clients[1], clients[2], clients[3], evidence)
        pg_port = (
            subprocess.run(
                [*prefix, "port", "postgres", "5432"],
                env=environment,
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            .stdout.strip()
            .rsplit(":", 1)[1]
        )
        dsn = (
            "postgresql+psycopg://fixture_admin:synthetic-admin-password@127.0.0.1:"
            + pg_port
            + "/marketplace_test"
        )
        qualify_application(endpoint, dsn, clients[0], evidence)
        evidence["passed"] = True
        return 0
    except (AssertionError, ClientError, RuntimeError, subprocess.SubprocessError) as error:
        evidence["qualification_error"] = str(error)
        return 1
    finally:
        for identity in clients:
            identity.close()
        subprocess.run(
            [*prefix, "down", "--volumes", "--remove-orphans"],
            env=environment,
            cwd=ROOT,
            check=True,
            timeout=90,
        )
        print(json.dumps(evidence, indent=2))
        if args.evidence_file:
            args.evidence_file.parent.mkdir(parents=True, exist_ok=True)
            args.evidence_file.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
