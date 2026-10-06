"""SQLAlchemy persistence for Stage H5 publisher trust (ADR 0036)."""

from __future__ import annotations

import time
from datetime import datetime

from sqlalchemy import Connection, Engine, insert, select, update
from sqlalchemy.engine import RowMapping

from nervos_core.application.publisher_trust import PublisherTrust
from nervos_core.infrastructure.database.models import PublisherTrustRecord
from nervos_core.infrastructure.database.transaction import TransactionRunner


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _view(row: PublisherTrustRecord | RowMapping) -> PublisherTrust:
    return PublisherTrust(
        signer_fingerprint=str(
            row["signer_fingerprint"] if isinstance(row, RowMapping) else row.signer_fingerprint
        ),
        state=str(row["state"] if isinstance(row, RowMapping) else row.state),
        decided_by=int(row["decided_by"] if isinstance(row, RowMapping) else row.decided_by),
        reason=row["reason"] if isinstance(row, RowMapping) else row.reason,
        source=str(row["source"] if isinstance(row, RowMapping) else row.source),
        created_at=row["created_at"] if isinstance(row, RowMapping) else row.created_at,
        updated_at=row["updated_at"] if isinstance(row, RowMapping) else row.updated_at,
    )


class SqlAlchemyPublisherTrustPersistence:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._transactions = TransactionRunner(engine, _sleep)

    def set_trust(
        self,
        *,
        signer_fingerprint: str,
        state: str,
        decided_by: int,
        reason: str | None,
        source: str,
        now: datetime,
    ) -> PublisherTrust:
        def write(conn: Connection) -> PublisherTrust:
            existing = (
                conn.execute(
                    select(PublisherTrustRecord).where(
                        PublisherTrustRecord.signer_fingerprint == signer_fingerprint
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is None:
                conn.execute(
                    insert(PublisherTrustRecord).values(
                        signer_fingerprint=signer_fingerprint,
                        state=state,
                        decided_by=decided_by,
                        reason=reason,
                        source=source,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                conn.execute(
                    update(PublisherTrustRecord)
                    .where(PublisherTrustRecord.signer_fingerprint == signer_fingerprint)
                    .values(
                        state=state,
                        decided_by=decided_by,
                        reason=reason,
                        source=source,
                        updated_at=now,
                    )
                )
            row = (
                conn.execute(
                    select(PublisherTrustRecord).where(
                        PublisherTrustRecord.signer_fingerprint == signer_fingerprint
                    )
                )
                .mappings()
                .one()
            )
            return _view(row)

        return self._transactions.run(write)

    def get_trust(self, signer_fingerprint: str) -> PublisherTrust | None:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    select(PublisherTrustRecord).where(
                        PublisherTrustRecord.signer_fingerprint == signer_fingerprint
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _view(row) if row is not None else None

    def list_trust(self) -> tuple[PublisherTrust, ...]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    select(PublisherTrustRecord).order_by(PublisherTrustRecord.signer_fingerprint)
                )
                .mappings()
                .all()
            )
        return tuple(_view(row) for row in rows)
