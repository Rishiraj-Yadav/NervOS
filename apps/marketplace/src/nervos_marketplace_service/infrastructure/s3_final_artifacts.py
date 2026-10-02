"""Conditional creation and full verification of an exact versioned object.

Conditional PUT resolves first-creation races. The returned VersionId, protected
from deletion by deployment policy, is the immutable locator. A key alone is not.
"""

from pathlib import Path

from botocore.exceptions import ClientError

from nervos_marketplace_service.domain.artifact_reference import (
    FinalizedArtifactRef,
    validate_version_id,
)
from nervos_marketplace_service.domain.errors import MarketplaceError
from nervos_marketplace_service.infrastructure.s3_artifact_store import S3ArtifactStore, object_key


class S3FinalArtifacts(S3ArtifactStore):
    """Compose with finalizer credentials, never public reader/admin credentials."""

    def require_versioning(self) -> None:
        try:
            if (
                self.client.get_bucket_versioning(Bucket=self.settings.s3_bucket).get("Status")
                != "Enabled"
            ):
                raise ValueError("Final bucket versioning must be enabled")
        except Exception:
            raise MarketplaceError("artifact_unavailable", 503) from None

    def create_final_if_absent(self, digest: str, path: Path, size: int) -> FinalizedArtifactRef:
        key = object_key(digest)
        self.require_versioning()
        if not 0 < size <= 256 * 1024**2 or path.stat().st_size != size:
            raise MarketplaceError("artifact_unavailable", 503)
        try:
            with path.open("rb") as source:
                try:
                    result = self.client.put_object(
                        Bucket=self.settings.s3_bucket,
                        Key=key,
                        Body=source,
                        ContentLength=size,
                        IfNoneMatch="*",
                    )
                    version = validate_version_id(result.get("VersionId", ""))
                except ClientError as error:
                    if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
                        raise
                    # Bounded crash/race recovery: observe only current version, then
                    # pin and fully verify it. Never scan/arbitrarily select history.
                    current = self.client.head_object(Bucket=self.settings.s3_bucket, Key=key)
                    if current["ContentLength"] != size:
                        raise ValueError("Existing artifact size mismatch") from None
                    version = validate_version_id(current.get("VersionId", ""))
            reference = FinalizedArtifactRef(digest, size, version)
            with_verified = self.open_verified(digest, size, version_id=version)
            with_verified.close()
            return reference
        except Exception:
            raise MarketplaceError("artifact_unavailable", 503) from None

    def available(self, reference: FinalizedArtifactRef) -> bool:
        try:
            stream = self.open_verified(
                reference.archive_sha256, reference.size_bytes, version_id=reference.version_id
            )
            stream.close()
            return True
        except MarketplaceError:
            return False
