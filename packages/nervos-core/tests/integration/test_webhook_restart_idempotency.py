"""Stage E5 focused traceability: webhook idempotency survives application recreation.

The E3 suites prove idempotency within one live ingress. This focused recovery test proves the
identity is durable: it commits through one composed ingress, tears its engine and services down,
and retries through a freshly composed ingress over the same disposable database -- the accepted
in-process representation of an API/application restart.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nervos_core.application.webhooks import (
    WebhookDelivery,
    WebhookDeliveryKind,
    WebhookDeliveryResult,
    WebhookDeliveryService,
)
from nervos_core.infrastructure.database import create_sqlite_engine
from nervos_core.infrastructure.database.triggers import SqlAlchemyTriggerPersistence
from scheduler_support import NOW
from sqlalchemy import Engine, text
from webhook_support import DEFAULT_BODY, build_rig, build_service


def _counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            name: int(connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())
            for name, table in {
                "occurrences": "trigger_occurrences",
                "runs": "runs",
            }.items()
        }


def _deliver(
    service: WebhookDeliveryService,
    *,
    public_id: str,
    secret: str,
    body: bytes,
    idempotency_key: str,
) -> WebhookDeliveryResult:
    return service.receive(
        WebhookDelivery(
            public_id=public_id,
            candidate_secret=secret,
            idempotency_key=idempotency_key,
            has_conflicting_idempotency_keys=False,
            is_json_content_type=True,
            occurred_at=NOW,
        ),
        body,
    )


def test_webhook_idempotency_authority_survives_an_application_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "nervos.db"

    # First process: provision and commit one durable delivery.
    first = build_rig(path, monkeypatch)
    issued = first.provision()
    public_id = issued.trigger.public_id or ""
    initial = first.deliver(issued, body=DEFAULT_BODY, idempotency_key="key-1")
    assert initial.kind is WebhookDeliveryKind.MATERIALIZED
    assert _counts(first.engine) == {"occurrences": 1, "runs": 1}
    first.engine.dispose()

    # Second process: new Engine, persistence and application service; same database file.
    restarted_engine = create_sqlite_engine(path)
    restarted_persistence = SqlAlchemyTriggerPersistence(restarted_engine, sleep=lambda _: None)
    restarted = build_service(restarted_persistence)
    try:
        duplicate = _deliver(
            restarted,
            public_id=public_id,
            secret=issued.secret,
            body=DEFAULT_BODY,
            idempotency_key="key-1",
        )
        assert duplicate.kind is WebhookDeliveryKind.DUPLICATE
        assert duplicate.duplicate is True
        assert duplicate.occurrence_id == initial.occurrence_id
        assert duplicate.run_id == initial.run_id
        assert _counts(restarted_engine) == {"occurrences": 1, "runs": 1}

        conflict = _deliver(
            restarted,
            public_id=public_id,
            secret=issued.secret,
            body=b'{"event":"different"}',
            idempotency_key="key-1",
        )
        assert conflict.kind is WebhookDeliveryKind.CONFLICT
        assert conflict.occurrence_id is None
        assert _counts(restarted_engine) == {"occurrences": 1, "runs": 1}
    finally:
        restarted_engine.dispose()
