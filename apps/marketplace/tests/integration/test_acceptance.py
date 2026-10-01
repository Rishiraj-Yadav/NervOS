"""Required real PostgreSQL 18 / authenticated S3 acceptance; no skip fallback."""

import hashlib
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from botocore import UNSIGNED
from botocore.config import Config as S3Config
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from nervos_marketplace_service.app import HostedReadiness, create_test_app
from nervos_marketplace_service.config import MarketplaceSettings
from nervos_marketplace_service.domain.errors import MarketplaceError
from nervos_marketplace_service.infrastructure.catalog_repository import PostgresCatalogRepository
from nervos_marketplace_service.infrastructure.database import SCHEMA_HEAD, create_hosted_engine
from nervos_marketplace_service.infrastructure.s3_artifact_store import (
    S3ArtifactStore,
    object_key,
    s3_client,
)
from pydantic import SecretStr
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from ..conftest import ROOT, insert_fixture
from ..fixtures import signed_package
from ..unit.test_semver_order import versions

pytestmark = pytest.mark.marketplace_integration


@pytest.fixture
def hosted_settings(database: Engine, tmp_path: Path) -> MarketplaceSettings:
    endpoint = os.environ.get("NERVOS_MARKETPLACE_TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.fail("Disposable authenticated S3 endpoint is required")
    return MarketplaceSettings(
        environment="test",
        database_dsn=SecretStr(
            database.url.set(
                username="mp_reader", password="synthetic-reader-password"
            ).render_as_string(hide_password=False)
        ),
        s3_endpoint_url=endpoint,
        s3_region="us-east-1",
        s3_bucket="marketplace-test",
        s3_access_key_id=SecretStr("fixture-reader"),
        s3_secret_access_key=SecretStr("synthetic-reader-password"),
        artifact_temp_directory=tmp_path,
    )


def admin_settings(settings: MarketplaceSettings) -> MarketplaceSettings:
    return settings.model_copy(
        update={
            "s3_access_key_id": SecretStr("fixture-admin"),
            "s3_secret_access_key": SecretStr("synthetic-admin-password"),
        }
    )


def test_migration_cycle(database: Engine) -> None:
    config = Config(str(ROOT / "apps/marketplace/alembic.ini"))
    os.environ["NERVOS_MARKETPLACE_DATABASE_DSN"] = database.url.render_as_string(
        hide_password=False
    )
    with database.connect() as connection:
        assert connection.execute(text("SHOW server_version")).scalar_one().startswith("18.")
        assert (
            connection.execute(
                text("SELECT version_num FROM marketplace_alembic_version")
            ).scalar_one()
            == SCHEMA_HEAD
        )
    assert set(inspect(database).get_table_names()) == {
        "package_projects",
        "package_listings",
        "package_releases",
        "artifacts",
        "marketplace_alembic_version",
    }
    command.downgrade(config, "base")
    assert inspect(database).get_table_names() == ["marketplace_alembic_version"]
    command.upgrade(config, "head")
    with database.begin() as connection:
        connection.execute(text("GRANT SELECT ON ALL TABLES IN SCHEMA public TO mp_reader"))
    assert any(
        index["name"] == "ix_listing_search"
        for index in inspect(database).get_indexes("package_listings")
    )


@pytest.mark.parametrize(
    "assignment",
    [
        "exact_version='2.0.0'",
        "content_digest='" + "b" * 64 + "'",
        "signer_fingerprint='" + "b" * 64 + "'",
        "manifest_bytes='changed'::bytea",
        "archive_sha256='" + "b" * 64 + "'",
        "manifest_version=2",
        "runtime_python='different'",
        "publication_state='ready',published_at=NULL",
        "published_at=published_at+interval '1 day'",
        "is_prerelease=true",
        "semver_key='changed'::bytea",
        "project_id='00000000-0000-0000-0000-000000000000'",
    ],
)
def test_published_immutable(database: Engine, assignment: str) -> None:
    insert_fixture(database, "com.acme.invoice")
    with pytest.raises(SQLAlchemyError), database.begin() as connection:
        # assignments are curated test constants, never public input.
        connection.execute(text("UPDATE package_releases SET " + assignment))


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM package_releases",
        "DELETE FROM artifacts",
        "DELETE FROM package_projects",
        "UPDATE package_projects SET package_id='com.other.invoice'",
        "UPDATE artifacts SET size_bytes=size_bytes+1",
    ],
)
def test_history_restrict(database: Engine, sql: str) -> None:
    insert_fixture(database, "com.acme.invoice")
    with pytest.raises(SQLAlchemyError), database.begin() as connection:
        connection.execute(text(sql))


def test_lifecycle(database: Engine) -> None:
    insert_fixture(database, "com.acme.invoice", publication="ready")
    with database.begin() as connection:
        connection.execute(
            text(
                "UPDATE package_releases SET publication_state='published',"
                "published_at=status_updated_at"
            )
        )
        for state in ("yanked", "available", "revoked"):
            connection.execute(
                text(
                    "UPDATE package_releases SET distribution_state=:state,"
                    "status_revision=status_revision+1"
                ),
                {"state": state},
            )
    with pytest.raises(SQLAlchemyError), database.begin() as connection:
        connection.execute(
            text(
                "UPDATE package_releases SET distribution_state='available',"
                "status_revision=status_revision+1"
            )
        )


@pytest.mark.parametrize("assignment", ["status_revision=1", "status_updated_at='2025-01-01'"])
def test_status_evidence_monotonic(database: Engine, assignment: str) -> None:
    insert_fixture(database, "com.acme.invoice")
    with database.begin() as connection:
        connection.execute(
            text("UPDATE package_releases SET distribution_state='yanked',status_revision=2")
        )
    with pytest.raises(SQLAlchemyError), database.begin() as connection:
        connection.execute(text("UPDATE package_releases SET " + assignment))


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE package_releases SET distribution_state='other'",
        "UPDATE package_releases SET status_revision=0",
        "UPDATE package_listings SET summary=repeat('x',4097)",
        "UPDATE package_listings SET description=repeat('x',32769)",
        "UPDATE package_listings SET display_name=repeat('x',257)",
        "UPDATE package_listings SET revision=0",
    ],
)
def test_checks(database: Engine, sql: str) -> None:
    insert_fixture(database, "com.acme.invoice", publication="ready")
    with pytest.raises(SQLAlchemyError), database.begin() as connection:
        connection.execute(text(sql))


def test_identity_race(database: Engine) -> None:
    barrier = Barrier(2)

    def claim() -> bool:
        barrier.wait(timeout=5)
        try:
            with database.begin() as connection:
                connection.execute(
                    text("INSERT INTO package_projects VALUES (:id,'com.race.test',now())"),
                    {"id": uuid4()},
                )
            return True
        except SQLAlchemyError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        assert sorted(future.result() for future in futures) == [False, True]


def test_exact_release_uniqueness(database: Engine) -> None:
    insert_fixture(database, "com.acme.invoice")
    with pytest.raises(SQLAlchemyError), database.begin() as connection:
        connection.execute(
            text("INSERT INTO artifacts SELECT archive_sha256,size_bytes,created_at FROM artifacts")
        )
    with pytest.raises(SQLAlchemyError), database.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO package_releases SELECT :id,project_id,exact_version,"
                "archive_sha256,content_digest,signer_fingerprint,manifest_version,"
                "manifest_bytes,runtime_language,runtime_python,nervos_min_version,"
                "nervos_max_version,semver_key,is_prerelease,publication_state,"
                "distribution_state,status_revision,published_at,status_updated_at "
                "FROM package_releases"
            ),
            {"id": uuid4()},
        )
    insert_fixture(database, "com.acme.invoice", "2.0.0", publication="ready")
    # Different artifact/PK isolates the project+exact-version unique constraint.
    with pytest.raises(SQLAlchemyError), database.begin() as connection:
        connection.execute(
            text("UPDATE package_releases SET exact_version='1.2.3' WHERE exact_version='2.0.0'")
        )


def test_publication_and_version_filters(database: Engine) -> None:
    insert_fixture(database, "com.acme.invoice", "1.0.0")
    insert_fixture(database, "com.acme.invoice", "2.0.0", state="yanked")
    insert_fixture(database, "com.acme.invoice", "3.0.0", state="revoked")
    insert_fixture(database, "com.acme.invoice", "4.0.0-alpha")
    insert_fixture(database, "com.acme.invoice", "5.0.0", publication="ready")
    repository = PostgresCatalogRepository(database)
    assert repository.package("com.acme.invoice").latest_stable_version == "1.0.0"
    assert [
        row.exact_version
        for row, _ in repository.versions("com.acme.invoice", 100, None, False, False)
    ] == ["1.0.0"]
    assert len(repository.versions("com.acme.invoice", 100, None, True, True)) == 4
    with pytest.raises(MarketplaceError) as error:
        repository.release("com.acme.invoice", "5.0.0")
    assert error.value.status == 404


def test_readiness_wrong_head_and_bucket(
    database: Engine, hosted_settings: MarketplaceSettings
) -> None:
    engine = create_hosted_engine(hosted_settings)
    store = S3ArtifactStore(hosted_settings)
    try:
        ready = HostedReadiness(engine, store)
        assert ready.ready()
        with database.begin() as connection:
            connection.execute(text("UPDATE marketplace_alembic_version SET version_num='wrong'"))
        assert not ready.ready()
        with database.begin() as connection:
            connection.execute(
                text("UPDATE marketplace_alembic_version SET version_num=:head"),
                {"head": SCHEMA_HEAD},
            )
        missing = S3ArtifactStore(
            hosted_settings.model_copy(update={"s3_bucket": "missing-bucket"})
        )
        try:
            assert not HostedReadiness(engine, missing).ready()
        finally:
            missing.close()
    finally:
        store.close()
        engine.dispose()


@pytest.mark.parametrize("dependency", ["database", "storage"])
def test_outage_health_and_safe_errors(
    database: Engine,
    hosted_settings: MarketplaceSettings,
    caplog: pytest.LogCaptureFixture,
    dependency: str,
) -> None:
    marker = "synthetic-private-dependency-marker"
    settings = (
        hosted_settings.model_copy(
            update={
                "database_dsn": SecretStr(
                    database.url.set(port=1, password=marker).render_as_string(hide_password=False)
                ),
                "db_connect_timeout_seconds": 1,
            }
        )
        if dependency == "database"
        else hosted_settings.model_copy(update={"s3_secret_access_key": SecretStr(marker)})
    )
    engine = create_hosted_engine(settings)
    store = S3ArtifactStore(settings)
    try:
        if dependency == "storage":
            insert_fixture(database, "com.acme.invoice")
        with TestClient(
            create_test_app(
                PostgresCatalogRepository(engine), store, HostedReadiness(engine, store)
            )
        ) as client:
            assert client.get("/health/live").status_code == 200
            response = client.get("/health/ready")
            assert response.status_code == 503
            assert marker not in response.text + str(response.headers) + caplog.text
            path = (
                "/marketplace/v1/packages"
                if dependency == "database"
                else ("/marketplace/v1/packages/com.acme.invoice/versions/1.2.3/artifact")
            )
            response = client.get(path)
            assert response.status_code == 503
            assert marker not in response.text + str(response.headers) + caplog.text
            assert response.json()["error"]["code"] in {
                "service_unavailable",
                "artifact_unavailable",
            }
    finally:
        store.close()
        engine.dispose()


def test_database_read_only(hosted_settings: MarketplaceSettings) -> None:
    engine = create_hosted_engine(hosted_settings)
    try:
        with pytest.raises(SQLAlchemyError), engine.begin() as connection:
            connection.execute(
                text("INSERT INTO package_projects VALUES (:id,'com.blocked.test',now())"),
                {"id": uuid4()},
            )
        # Role privileges also deny writes without the application's read-only session setting.
        independent = create_engine(hosted_settings.database_dsn.get_secret_value())
        try:
            with pytest.raises(SQLAlchemyError), independent.begin() as connection:
                connection.execute(text("DELETE FROM package_projects"))
        finally:
            independent.dispose()
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "query", ["Invoice", "invoice", "Accounting", "reconciliation", "com.acme.invoice"]
)
def test_search(database: Engine, query: str) -> None:
    insert_fixture(database, "com.acme.invoice")
    rows = PostgresCatalogRepository(database).packages(query, 21, None)
    assert [item.package_id for item, _ in rows] == ["com.acme.invoice"]


def test_search_and_pagination(database: Engine) -> None:
    from nervos_marketplace_service.api.cursors import decode, encode, fingerprint

    for package in ("com.acme.zeta", "com.acme.invoice", "com.acme.alpha"):
        insert_fixture(database, package)
    repository = PostgresCatalogRepository(database)
    first = repository.packages("", 2, None)
    second = repository.packages("", 2, first[-1][1])
    assert [item.package_id for item, _ in first + second] == [
        "com.acme.alpha",
        "com.acme.invoice",
        "com.acme.zeta",
    ]
    assert repository.packages("com.acme.invoice", 2, None)[0][0].package_id == "com.acme.invoice"
    for hostile in ("' OR 1=1;--", "invoice | !robot", "*"):
        repository.packages(hostile, 21, None)
    raw = encode(first[0][1], "packages", fingerprint("packages", "invoice"))
    with pytest.raises(MarketplaceError):
        decode(raw, "packages", fingerprint("packages", "other"))


def test_search_maximum_listing_and_rank_cursor(database: Engine) -> None:
    from nervos_marketplace_service.api.cursors import decode, encode, fingerprint
    from nervos_marketplace_service.application.ports import CatalogPosition

    insert_fixture(database, "com.acme.invoice")
    with database.begin() as connection:
        connection.execute(
            text(
                "UPDATE package_listings SET display_name=repeat('a ',128),"
                "summary=repeat('a ',2048),description=repeat('a ',16384)"
            )
        )
    rows = PostgresCatalogRepository(database).packages("a", 2, None)
    assert len(rows) == 1 and rows[0][1].rank > 0
    position = CatalogPosition(package_id="com.acme.invoice", rank=9223372036854775807)
    binding = fingerprint("packages", "a")
    assert decode(encode(position, "packages", binding), "packages", binding) == position


def test_sql_semver(database: Engine) -> None:
    from functools import cmp_to_key

    from nervos_core.domain.packages import PackageVersion

    all_versions = [str(version) for version in versions()]
    for version in all_versions:
        insert_fixture(database, "com.acme.invoice", version)
    repository = PostgresCatalogRepository(database)
    actual: list[str] = []
    after = None
    while page := repository.versions("com.acme.invoice", 100, after, False, True):
        actual.extend(item.exact_version for item, _ in page)
        after = page[-1][1]

    def compare(a: str, b: str) -> int:
        left, right = PackageVersion(a), PackageVersion(b)
        return 1 if left < right else -1 if right < left else (a > b) - (a < b)

    assert actual == sorted(all_versions, key=cmp_to_key(compare))
    page = repository.versions("com.acme.invoice", 3, None, False, True)
    next_page = repository.versions("com.acme.invoice", 3, page[-1][1], False, True)
    assert [item.exact_version for item, _ in page + next_page] == actual[:6]
    assert repository.package("com.acme.invoice").latest_stable_version == "9" * 55 + ".0.0"


def test_storage_permissions(hosted_settings: MarketplaceSettings) -> None:
    admin = s3_client(admin_settings(hosted_settings))
    reader = s3_client(hosted_settings)
    key = object_key(hashlib.sha256(b"permissions").hexdigest())
    try:
        admin.put_object(Bucket=hosted_settings.s3_bucket, Key=key, Body=b"permissions")
        assert (
            reader.get_object(Bucket=hosted_settings.s3_bucket, Key=key)["Body"].read()
            == b"permissions"
        )
        with pytest.raises(ClientError):
            reader.put_object(Bucket=hosted_settings.s3_bucket, Key=key, Body=b"replacement")
        with pytest.raises(ClientError):
            reader.delete_object(Bucket=hosted_settings.s3_bucket, Key=key)
        import boto3

        unsigned = boto3.client(  # pyright: ignore[reportUnknownMemberType]  # S3-only stubs
            "s3",
            endpoint_url=hosted_settings.s3_endpoint_url,
            config=S3Config(
                signature_version=UNSIGNED, retries={"total_max_attempts": 1}, proxies={}
            ),
        )
        try:
            with pytest.raises(ClientError):
                unsigned.get_object(Bucket=hosted_settings.s3_bucket, Key=key)
        finally:
            unsigned.close()
    finally:
        admin.close()
        reader.close()


def test_conditional_create(hosted_settings: MarketplaceSettings) -> None:
    client = s3_client(admin_settings(hosted_settings))
    key = "conditional-test/" + uuid4().hex
    barrier = Barrier(2)

    def put() -> int:
        barrier.wait(timeout=5)
        try:
            return client.put_object(
                Bucket=hosted_settings.s3_bucket, Key=key, Body=b"exact", IfNoneMatch="*"
            )["ResponseMetadata"]["HTTPStatusCode"]
        except ClientError as error:
            return error.response.get("ResponseMetadata", {})["HTTPStatusCode"]

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(put) for _ in range(2)]
            assert sorted(future.result() for future in futures) == [200, 412]
    finally:
        client.close()


@pytest.mark.parametrize("mutation", ["valid", "wrong", "short", "long", "missing"])
def test_api_storage_bytes(
    database: Engine, hosted_settings: MarketplaceSettings, mutation: str
) -> None:
    fixture = signed_package()
    digest = insert_fixture(database, "com.acme.invoice")
    admin = s3_client(admin_settings(hosted_settings))
    key = object_key(digest)
    data = fixture.raw
    if mutation == "wrong":
        data = b"x" + data[1:]
    if mutation == "short":
        data = data[:-1]
    if mutation == "long":
        data += b"x"
    if mutation == "missing":
        admin.delete_object(Bucket=hosted_settings.s3_bucket, Key=key)
    else:
        admin.put_object(Bucket=hosted_settings.s3_bucket, Key=key, Body=data)
    engine = create_hosted_engine(hosted_settings)
    store = S3ArtifactStore(hosted_settings)
    try:
        with TestClient(
            create_test_app(
                PostgresCatalogRepository(engine), store, HostedReadiness(engine, store)
            )
        ) as client:
            assert client.get("/health/live").status_code == 200
            assert client.get("/health/ready").status_code == 200
            response = client.get(
                "/marketplace/v1/packages/com.acme.invoice/versions/1.2.3/artifact"
            )
            if mutation == "valid":
                assert response.content == fixture.raw
                assert hashlib.sha256(response.content).hexdigest() == digest
                print(
                    "Artifact byte proof:",
                    digest,
                    "==",
                    hashlib.sha256(response.content).hexdigest(),
                )
            else:
                assert response.status_code == 503
                assert response.json()["error"]["code"] == "artifact_unavailable"
            assert not list(hosted_settings.artifact_temp_directory.iterdir())
            assert "acme_invoice.agent" not in sys.modules
    finally:
        store.close()
        engine.dispose()
        admin.close()


def test_real_server_smoke(database: Engine, hosted_settings: MarketplaceSettings) -> None:
    fixture = signed_package()
    digest = hashlib.sha256(fixture.raw).hexdigest()
    admin = s3_client(admin_settings(hosted_settings))
    admin.put_object(Bucket=hosted_settings.s3_bucket, Key=object_key(digest), Body=fixture.raw)
    admin.close()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    environment = os.environ.copy()
    for name in MarketplaceSettings.model_fields:
        value = getattr(hosted_settings, name)
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if value is not None:
            environment["NERVOS_MARKETPLACE_" + name.upper()] = str(value)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "nervos_marketplace_service.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", timeout=2, trust_env=False
        ) as client:
            deadline = time.monotonic() + 30
            while True:
                try:
                    if client.get("/health/ready").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if process.poll() is not None or time.monotonic() > deadline:
                    pytest.fail("Hosted server did not become ready")
                time.sleep(0.2)
            assert client.get("/health/live").status_code == 200
            assert client.get("/marketplace/v1/packages").json()["items"] == []
            insert_fixture(database, "com.acme.invoice")
            assert len(client.get("/marketplace/v1/packages").json()["items"]) == 1
            assert (
                client.get(
                    "/marketplace/v1/packages/com.acme.invoice/versions/1.2.3/artifact"
                ).content
                == fixture.raw
            )
    finally:
        process.terminate()
        process.wait(timeout=15)
