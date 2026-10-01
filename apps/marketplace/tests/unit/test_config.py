from pathlib import Path

import pytest
from nervos_marketplace_service.config import MarketplaceSettings
from pydantic import ValidationError


def test_secret_settings(settings: MarketplaceSettings) -> None:
    for marker in ("fixture-reader", "synthetic-reader-password", "fixture:fixture"):
        assert marker not in repr(settings)
        assert marker not in str(settings.model_dump())
        assert marker not in settings.model_dump_json()
    assert settings.model_config.get("env_file") is None


@pytest.mark.parametrize(
    "changes",
    [
        {"database_dsn": "sqlite:///runtime.db"},
        {"environment": "production"},
        {"s3_endpoint_url": "http://192.168.1.2:8333"},
        {"s3_endpoint_url": "https://user:password@example.com"},
        {"s3_endpoint_url": "https://example.com/path"},
        {"max_concurrent_artifact_reads": 0},
        {"db_pool_size": 1000},
        {"artifact_operation_timeout_seconds": 1000},
        {"s3_secret_access_key": " "},
    ],
)
def test_invalid_config(
    settings: MarketplaceSettings, changes: dict[str, object], tmp_path: Path
) -> None:
    values: dict[str, object] = {
        "environment": "test",
        "database_dsn": settings.database_dsn,
        "s3_endpoint_url": settings.s3_endpoint_url,
        "s3_region": settings.s3_region,
        "s3_bucket": settings.s3_bucket,
        "s3_access_key_id": settings.s3_access_key_id,
        "s3_secret_access_key": settings.s3_secret_access_key,
        "artifact_temp_directory": tmp_path,
    }
    values.update(changes)
    with pytest.raises(ValidationError) as error:
        MarketplaceSettings.model_validate(values)
    assert "synthetic-reader-password" not in str(error.value)
