import io
from pathlib import Path

import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber
from nervos_marketplace_service.config import MarketplaceSettings
from nervos_marketplace_service.domain.errors import MarketplaceError
from nervos_marketplace_service.infrastructure.s3_artifact_store import S3ArtifactStore, object_key

from ..fixtures import signed_package


class MemoryBody(StreamingBody):
    def set_socket_timeout(self, timeout: float) -> None:
        del timeout


@pytest.mark.parametrize("mutation", ["valid", "wrong", "short", "long", "declared", "missing"])
def test_staged_verification(settings: MarketplaceSettings, tmp_path: Path, mutation: str) -> None:
    fixture = signed_package()
    data = fixture.raw
    if mutation == "wrong":
        data = b"x" + data[1:]
    elif mutation == "short":
        data = data[:-1]
    elif mutation in {"long", "declared"}:
        data += b"x"
    store = S3ArtifactStore(settings)
    digest = fixture.verified.archive_digest
    params = {"Bucket": settings.s3_bucket, "Key": object_key(digest)}
    with Stubber(store.client) as stub:
        if mutation == "missing":
            stub.add_client_error(
                "head_object", "NoSuchKey", http_status_code=404, expected_params=params
            )
        else:
            stub.add_response(
                "head_object",
                {
                    "ContentLength": len(data) if mutation == "declared" else len(fixture.raw),
                    "ETag": "NOT-A-SHA256",
                },
                params,
            )
            if mutation != "declared":
                stub.add_response(
                    "get_object",
                    {
                        "ContentLength": len(fixture.raw),
                        "Body": MemoryBody(io.BytesIO(data), len(fixture.raw)),
                    },
                    params,
                )
        if mutation == "valid":
            stream = store.open_verified(digest, len(fixture.raw))
            assert b"".join(stream.chunks()) == fixture.raw
            stream.close()
        else:
            with pytest.raises(MarketplaceError) as error:
                store.open_verified(digest, len(fixture.raw))
            assert error.value.code == "artifact_unavailable"
        stub.assert_no_pending_responses()
    assert not list(tmp_path.iterdir())
    store.close()


def test_admission(settings: MarketplaceSettings) -> None:
    store = S3ArtifactStore(settings)
    for _ in range(settings.max_concurrent_artifact_reads):
        assert store.admission.acquire(False)
    with pytest.raises(MarketplaceError) as error:
        store.open_verified("a" * 64, 1)
    assert error.value.status == 429
    for _ in range(settings.max_concurrent_artifact_reads):
        store.admission.release()
    store.close()


@pytest.mark.parametrize("digest", ["../", "A" * 64, "a" * 63, "a" * 64 + "/", "\\.."])
def test_digest_paths(digest: str) -> None:
    with pytest.raises(ValueError):
        object_key(digest)
