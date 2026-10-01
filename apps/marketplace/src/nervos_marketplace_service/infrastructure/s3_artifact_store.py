"""Private S3 reads with complete transport verification before HTTP delivery."""

import hashlib
import os
import shutil
import tempfile
import time
from pathlib import Path
from threading import BoundedSemaphore, Lock
from typing import TYPE_CHECKING

import boto3
from botocore.config import Config
from nervos_core.domain.package_installation import validate_sha256

from nervos_marketplace_service.application.ports import ArtifactStat
from nervos_marketplace_service.config import MarketplaceSettings
from nervos_marketplace_service.domain.catalog import MAX_ARCHIVE_BYTES
from nervos_marketplace_service.domain.errors import MarketplaceError
from nervos_marketplace_service.infrastructure.verified_stream import StagedArtifact

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client


def object_key(digest: str) -> str:
    validate_sha256(digest)
    return f"artifacts/sha256/{digest[:2]}/{digest}.nervos"


def s3_client(settings: MarketplaceSettings) -> "S3Client":
    # Explicit credentials and disabled proxies prevent ambient credential/proxy discovery.
    # Only S3 stubs are installed; unrelated boto3 overloads have unknown returns.
    return boto3.client(  # pyright: ignore[reportUnknownMemberType]
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key_id.get_secret_value(),
        aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
        aws_session_token=(
            settings.s3_session_token.get_secret_value() if settings.s3_session_token else None
        ),
        config=Config(
            connect_timeout=settings.s3_connect_timeout_seconds,
            read_timeout=settings.s3_read_timeout_seconds,
            retries={"total_max_attempts": 1, "mode": "standard"},
            proxies={},
            s3={"addressing_style": settings.s3_addressing_style},
        ),
    )


class S3ArtifactStore:
    def __init__(self, settings: MarketplaceSettings, client: "S3Client | None" = None) -> None:
        self.settings = settings
        self.root = settings.artifact_temp_directory.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.client = client or s3_client(settings)
        self.admission = BoundedSemaphore(settings.max_concurrent_artifact_reads)
        self.lock = Lock()
        self.streams: set[StagedArtifact] = set()
        self.closed = False

    def stat(self, archive_sha256: str) -> ArtifactStat:
        key = object_key(archive_sha256)
        try:
            result = self.client.head_object(Bucket=self.settings.s3_bucket, Key=key)
            return ArtifactStat(result["ContentLength"])
        except Exception:
            raise MarketplaceError("artifact_unavailable", 503) from None

    def ready(self) -> bool:
        try:
            self.client.head_bucket(Bucket=self.settings.s3_bucket)
            # Readiness may write a PRIVATE LOCAL probe, never DB/object-store data.
            with tempfile.TemporaryFile(dir=self.root) as probe:
                probe.write(b"ready")
            return shutil.disk_usage(self.root).free >= MAX_ARCHIVE_BYTES
        except Exception:
            return False

    def open_verified(self, archive_sha256: str, expected_size: int) -> StagedArtifact:
        key = object_key(archive_sha256)
        if not 0 < expected_size <= MAX_ARCHIVE_BYTES:
            raise MarketplaceError("artifact_unavailable", 503)
        if not self.admission.acquire(blocking=False):
            raise MarketplaceError("rate_limited", 429)
        path: Path | None = None
        file = None
        deadline = time.monotonic() + self.settings.artifact_operation_timeout_seconds
        try:
            if self.closed or shutil.disk_usage(self.root).free < (
                MAX_ARCHIVE_BYTES * self.settings.max_concurrent_artifact_reads
            ):
                raise MarketplaceError("artifact_unavailable", 503)
            if self.stat(archive_sha256).size_bytes != expected_size:
                raise MarketplaceError("artifact_unavailable", 503)
            response = self.client.get_object(Bucket=self.settings.s3_bucket, Key=key)
            body = response["Body"]
            try:
                if response["ContentLength"] != expected_size:
                    raise MarketplaceError("artifact_unavailable", 503)
                file = tempfile.NamedTemporaryFile(  # noqa: SIM115 -- ownership transfers to StagedArtifact
                    mode="w+b", dir=self.root, prefix="mp-read-", delete=False
                )
                path = Path(file.name)
                digest = hashlib.sha256()
                size = 0
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise MarketplaceError("artifact_unavailable", 503)
                    # urllib3 releases its socket after consuming Content-Length. The
                    # final EOF check has no network read left and must not touch it.
                    if size < expected_size:
                        body.set_socket_timeout(
                            min(self.settings.s3_read_timeout_seconds, remaining)
                        )
                    chunk = body.read(min(65536, expected_size - size + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > expected_size:
                        raise MarketplaceError("artifact_unavailable", 503)
                    digest.update(chunk)
                    file.write(chunk)
                if size != expected_size or digest.hexdigest() != archive_sha256:
                    raise MarketplaceError("artifact_unavailable", 503)
                file.flush()
                os.fsync(file.fileno())
                if time.monotonic() > deadline:
                    raise MarketplaceError("artifact_unavailable", 503)
                file.seek(0)
            finally:
                body.close()
            stream: StagedArtifact

            def release() -> None:
                with self.lock:
                    self.streams.discard(stream)
                self.admission.release()

            stream = StagedArtifact(file, path, size, archive_sha256, release)
            with self.lock:
                if self.closed:
                    raise MarketplaceError("artifact_unavailable", 503)
                self.streams.add(stream)
            return stream
        except BaseException as error:
            try:
                if file is not None:
                    file.close()
                if path is not None:
                    path.unlink(missing_ok=True)
            finally:
                self.admission.release()
            if isinstance(error, MarketplaceError):
                raise
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise MarketplaceError("artifact_unavailable", 503) from None

    def close(self) -> None:
        with self.lock:
            self.closed = True
            streams = list(self.streams)
        for stream in streams:
            stream.close()
        self.client.close()
