"""Separate quarantine and finalizer credentials for immutable publication."""

import hashlib
from pathlib import Path
from uuid import UUID

from nervos_core.domain.package_installation import validate_sha256

from nervos_marketplace_service.config import MarketplaceSettings
from nervos_marketplace_service.domain.artifact_reference import FinalizedArtifactRef
from nervos_marketplace_service.domain.errors import MarketplaceError
from nervos_marketplace_service.infrastructure.s3_artifact_store import s3_client
from nervos_marketplace_service.infrastructure.s3_final_artifacts import S3FinalArtifacts


class S3PublicationStorage:
    def __init__(self, settings: MarketplaceSettings) -> None:
        if not all(
            (
                settings.quarantine_access_key_id,
                settings.quarantine_secret_access_key,
                settings.finalizer_access_key_id,
                settings.finalizer_secret_access_key,
            )
        ):
            raise ValueError("Separate publication storage credentials required")
        self.settings = settings
        self.quarantine_client = s3_client(
            settings.model_copy(
                update={
                    "s3_access_key_id": settings.quarantine_access_key_id,
                    "s3_secret_access_key": settings.quarantine_secret_access_key,
                    "s3_session_token": None,
                }
            )
        )
        self.finalizer = S3FinalArtifacts(
            settings.model_copy(
                update={
                    "s3_access_key_id": settings.finalizer_access_key_id,
                    "s3_secret_access_key": settings.finalizer_secret_access_key,
                    "s3_session_token": None,
                }
            )
        )

    @staticmethod
    def key(operation_id: UUID, digest: str) -> str:
        validate_sha256(digest)
        return f"quarantine/{operation_id}/{digest}.nervos"

    def quarantine(self, operation_id: UUID, digest: str, path: Path, size: int) -> None:
        with path.open("rb") as source:
            self.quarantine_client.put_object(
                Bucket=self.settings.s3_bucket,
                Key=self.key(operation_id, digest),
                Body=source,
                ContentLength=size,
            )

    def retrieve(self, operation_id: UUID, digest: str, size: int, destination: Path) -> None:
        result = self.quarantine_client.get_object(
            Bucket=self.settings.s3_bucket, Key=self.key(operation_id, digest)
        )
        source = result["Body"]
        try:
            if result["ContentLength"] != size or not 0 < size <= 256 * 1024**2:
                raise MarketplaceError("artifact_unavailable", 503)
            hashed, count = hashlib.sha256(), 0
            with destination.open("xb") as output:
                while chunk := source.read(65536):
                    count += len(chunk)
                    if count > size:
                        raise MarketplaceError("artifact_unavailable", 503)
                    hashed.update(chunk)
                    output.write(chunk)
            if count != size or hashed.hexdigest() != digest:
                raise MarketplaceError("artifact_unavailable", 503)
        finally:
            source.close()

    def finalize(self, digest: str, path: Path, size: int) -> FinalizedArtifactRef:
        return self.finalizer.create_final_if_absent(digest, path, size)

    def available(self, reference: FinalizedArtifactRef) -> bool:
        return self.finalizer.available(reference)

    def delete_quarantine(self, operation_id: UUID, digest: str) -> None:
        self.quarantine_client.delete_object(
            Bucket=self.settings.s3_bucket, Key=self.key(operation_id, digest)
        )

    def close(self) -> None:
        self.quarantine_client.close()
        self.finalizer.close()
