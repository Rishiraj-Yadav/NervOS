import hashlib
import io
from pathlib import Path

import pytest
from botocore.stub import ANY, Stubber
from nervos_marketplace_service.config import MarketplaceSettings
from nervos_marketplace_service.domain.artifact_reference import FinalizedArtifactRef
from nervos_marketplace_service.domain.errors import MarketplaceError
from nervos_marketplace_service.infrastructure.s3_artifact_store import object_key
from nervos_marketplace_service.infrastructure.s3_final_artifacts import S3FinalArtifacts

from .test_artifact_reads import MemoryBody


@pytest.mark.parametrize("version", ["", "null", "a" * 1025, "x\nheader", "x\\id", " x "])
def test_version_evidence_validation(version: str) -> None:
    with pytest.raises(ValueError):
        FinalizedArtifactRef("a" * 64, 1, version)


def test_version_is_opaque_query_evidence_never_a_filesystem_path() -> None:
    reference = FinalizedArtifactRef("a" * 64, 1, "../opaque")
    assert reference.version_id == "../opaque"
    assert reference.key == "artifacts/sha256/aa/" + "a" * 64 + ".nervos"


def stage(
    stub: Stubber,
    settings: MarketplaceSettings,
    digest: str,
    data: bytes,
    version: str,
    *,
    mismatch: bool = False,
) -> None:
    params = {"Bucket": settings.s3_bucket, "Key": object_key(digest), "VersionId": version}
    stub.add_response("head_object", {"ContentLength": len(data), "VersionId": version}, params)
    stub.add_response(
        "get_object",
        {
            "ContentLength": len(data),
            "VersionId": "other" if mismatch else version,
            "Body": MemoryBody(io.BytesIO(data), len(data)),
        },
        params,
    )


@pytest.mark.parametrize("adopt", [False, True])
def test_conditional_create_or_verified_adoption(
    settings: MarketplaceSettings, tmp_path: Path, adopt: bool
) -> None:
    data = b"verified artifact"
    digest = hashlib.sha256(data).hexdigest()
    path = tmp_path / "source"
    path.write_bytes(data)
    store = S3FinalArtifacts(settings)
    params = {"Bucket": settings.s3_bucket, "Key": object_key(digest)}
    with Stubber(store.client) as stub:
        stub.add_response(
            "get_bucket_versioning", {"Status": "Enabled"}, {"Bucket": settings.s3_bucket}
        )
        upload = {**params, "Body": ANY, "ContentLength": len(data), "IfNoneMatch": "*"}
        if adopt:
            stub.add_client_error(
                "put_object", "PreconditionFailed", http_status_code=412, expected_params=upload
            )
            stub.add_response(
                "head_object", {"ContentLength": len(data), "VersionId": "V1"}, params
            )
        else:
            stub.add_response("put_object", {"VersionId": "V1"}, upload)
        stage(stub, settings, digest, data, "V1")
        ref = store.create_final_if_absent(digest, path, len(data))
        assert ref == FinalizedArtifactRef(digest, len(data), "V1")
        assert ref.key == object_key(digest)
        stub.assert_no_pending_responses()
    store.close()
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("response", [{}, {"Status": "Suspended"}])
def test_unversioned_finalization_fails_before_write(
    settings: MarketplaceSettings, tmp_path: Path, response: dict[str, str]
) -> None:
    store = S3FinalArtifacts(settings)
    with Stubber(store.client) as stub:
        stub.add_response("get_bucket_versioning", response, {"Bucket": settings.s3_bucket})
        with pytest.raises(MarketplaceError):
            store.create_final_if_absent("a" * 64, tmp_path / "absent", 1)
        stub.assert_no_pending_responses()
    store.close()


@pytest.mark.parametrize("mismatch", [False, True])
def test_pinned_read_never_uses_default_version(
    settings: MarketplaceSettings, mismatch: bool
) -> None:
    data = b"original bytes"
    digest = hashlib.sha256(data).hexdigest()
    store = S3FinalArtifacts(settings)
    with Stubber(store.client) as stub:
        stage(stub, settings, digest, data, "V1", mismatch=mismatch)
        if mismatch:
            with pytest.raises(MarketplaceError):
                store.open_verified(digest, len(data), version_id="V1")
        else:
            stream = store.open_verified(digest, len(data), version_id="V1")
            assert b"".join(stream.chunks()) == data
            stream.close()
        stub.assert_no_pending_responses()
    store.close()


def test_wrong_current_version_not_adopted(settings: MarketplaceSettings, tmp_path: Path) -> None:
    data, wrong = b"original", b"modified"
    digest = hashlib.sha256(data).hexdigest()
    path = tmp_path / "source"
    path.write_bytes(data)
    store = S3FinalArtifacts(settings)
    params = {"Bucket": settings.s3_bucket, "Key": object_key(digest)}
    with Stubber(store.client) as stub:
        stub.add_response(
            "get_bucket_versioning", {"Status": "Enabled"}, {"Bucket": settings.s3_bucket}
        )
        stub.add_client_error(
            "put_object",
            "PreconditionFailed",
            http_status_code=412,
            expected_params={**params, "Body": ANY, "ContentLength": len(data), "IfNoneMatch": "*"},
        )
        stub.add_response("head_object", {"ContentLength": len(data), "VersionId": "V2"}, params)
        stage(stub, settings, digest, wrong, "V2")
        with pytest.raises(MarketplaceError):
            store.create_final_if_absent(digest, path, len(data))
        stub.assert_no_pending_responses()
    store.close()
    assert list(tmp_path.iterdir()) == [path]
