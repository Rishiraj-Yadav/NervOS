"""Own disposable PostgreSQL/S3 processes; never use the developer runtime database."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    docker = shutil.which("docker")
    if docker is None:
        raise SystemExit("Marketplace integration requires Docker with its Linux engine running")
    prefix = [
        docker,
        "compose",
        "-f",
        str(ROOT / "apps/marketplace/compose.test.yml"),
        "-p",
        "nervos-i1-" + uuid4().hex[:12],
    ]
    environment = os.environ.copy()
    environment.update({"MP_TEST_PG_PORT": "0", "MP_TEST_S3_PORT": "0"})
    try:
        subprocess.run([*prefix, "up", "-d"], cwd=ROOT, env=environment, check=True)

        def port(service: str, internal: str) -> int:
            result = subprocess.run(
                [*prefix, "port", service, internal],
                cwd=ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            return int(result.stdout.strip().rsplit(":", 1)[1])

        pg_port, s3_port = port("postgres", "5432"), port("s3", "8333")
        environment.update(
            {
                "NERVOS_MARKETPLACE_TEST_DATABASE_DSN": f"postgresql+psycopg://fixture_admin:synthetic-admin-password@127.0.0.1:{pg_port}/marketplace_test",
                "NERVOS_MARKETPLACE_TEST_S3_ENDPOINT": f"http://127.0.0.1:{s3_port}",
            }
        )
        import boto3
        import psycopg
        from botocore.config import Config

        client = boto3.client(  # pyright: ignore[reportUnknownMemberType]  # S3-only stubs
            "s3",
            endpoint_url=environment["NERVOS_MARKETPLACE_TEST_S3_ENDPOINT"],
            region_name="us-east-1",
            aws_access_key_id="fixture-admin",
            aws_secret_access_key="synthetic-admin-password",
            config=Config(
                connect_timeout=1, read_timeout=1, retries={"total_max_attempts": 1}, proxies={}
            ),
        )
        deadline = time.monotonic() + 90
        while True:
            try:
                with psycopg.connect(
                    environment["NERVOS_MARKETPLACE_TEST_DATABASE_DSN"].replace(
                        "postgresql+psycopg://", "postgresql://", 1
                    ),
                    connect_timeout=1,
                ):
                    client.head_bucket(Bucket="marketplace-test")
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise SystemExit(
                        "Disposable Marketplace services failed bounded readiness"
                    ) from None
                time.sleep(1)
        client.put_bucket_versioning(
            Bucket="marketplace-test", VersioningConfiguration={"Status": "Enabled"}
        )
        client.close()
        print("Disposable PostgreSQL 18 and authenticated S3 ready", flush=True)
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "apps/marketplace/tests/integration",
                "-m",
                "marketplace_integration",
                "-v",
            ],
            cwd=ROOT,
            env=environment,
            check=False,
        ).returncode
    finally:
        subprocess.run(
            [*prefix, "down", "--volumes", "--remove-orphans"],
            cwd=ROOT,
            env=environment,
            check=False,
        )


if __name__ == "__main__":
    raise SystemExit(main())
