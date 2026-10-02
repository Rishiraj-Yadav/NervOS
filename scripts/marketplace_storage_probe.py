"""Probe pinned S3 immutable-write policy using disposable test-owned services."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    docker = shutil.which("docker")
    if docker is None:
        raise SystemExit("Disposable storage probe requires Docker")
    prefix = [
        docker,
        "compose",
        "-f",
        str(ROOT / "apps/marketplace/compose.test.yml"),
        "-p",
        "nervos-storage-probe-" + uuid4().hex[:12],
    ]
    environment = {**os.environ, "MP_TEST_PG_PORT": "0", "MP_TEST_S3_PORT": "0"}
    try:
        subprocess.run([*prefix, "up", "-d"], env=environment, cwd=ROOT, check=True)
        port = (
            subprocess.run(
                [*prefix, "port", "s3", "8333"],
                env=environment,
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .rsplit(":", 1)[1]
        )
        client = boto3.client(  # pyright: ignore[reportUnknownMemberType]
            "s3",
            endpoint_url=f"http://127.0.0.1:{port}",
            region_name="us-east-1",
            aws_access_key_id="fixture-admin",
            aws_secret_access_key="synthetic-admin-password",
            config=Config(
                connect_timeout=1, read_timeout=3, retries={"total_max_attempts": 1}, proxies={}
            ),
        )
        deadline = time.monotonic() + 60
        while True:
            try:
                client.head_bucket(Bucket="marketplace-test")
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise RuntimeError("Disposable S3 readiness failed") from None
                time.sleep(1)
        key = "immutability-probe/" + uuid4().hex
        writer = boto3.client(  # pyright: ignore[reportUnknownMemberType]
            "s3",
            endpoint_url=f"http://127.0.0.1:{port}",
            region_name="us-east-1",
            aws_access_key_id="fixture-finalizer-probe",
            aws_secret_access_key="synthetic-finalizer-probe-password",
            config=Config(
                connect_timeout=1, read_timeout=3, retries={"total_max_attempts": 1}, proxies={}
            ),
        )
        writer.put_object(Bucket="marketplace-test", Key=key, Body=b"original", IfNoneMatch="*")
        results: dict[str, object] = {}
        try:
            writer.put_object(
                Bucket="marketplace-test", Key=key, Body=b"replacement", IfNoneMatch="*"
            )
            results["conditional_second_create"] = "unexpected success"
        except ClientError as error:
            results["conditional_second_create"] = error.response.get("ResponseMetadata", {}).get(
                "HTTPStatusCode"
            )
        client.put_bucket_policy(
            Bucket="marketplace-test",
            Policy=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Deny",
                            "Principal": "*",
                            "Action": "s3:PutObject",
                            "Resource": "arn:aws:s3:::marketplace-test/immutability-probe/*",
                            "Condition": {"Null": {"s3:if-none-match": "true"}},
                        }
                    ],
                }
            ),
        )
        for label, conditional in (
            ("conditional_create_with_policy", True),
            ("unconditional_create_with_policy", False),
        ):
            try:
                if conditional:
                    writer.put_object(
                        Bucket="marketplace-test",
                        Key=key + "-" + label,
                        Body=b"original",
                        IfNoneMatch="*",
                    )
                else:
                    writer.put_object(
                        Bucket="marketplace-test", Key=key + "-" + label, Body=b"original"
                    )
                results[label] = "success"
            except ClientError as error:
                results[label] = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        client.delete_bucket_policy(Bucket="marketplace-test")
        writer.put_object(Bucket="marketplace-test", Key=key, Body=b"replacement")
        body = writer.get_object(Bucket="marketplace-test", Key=key)["Body"]
        try:
            results["unconditional_overwrite_without_policy"] = body.read() == b"replacement"
        finally:
            body.close()
            writer.close()
            client.close()
        print(json.dumps(results, indent=2))
        return 0
    finally:
        subprocess.run(
            [*prefix, "down", "--volumes", "--remove-orphans"],
            env=environment,
            cwd=ROOT,
            check=False,
        )


if __name__ == "__main__":
    raise SystemExit(main())
