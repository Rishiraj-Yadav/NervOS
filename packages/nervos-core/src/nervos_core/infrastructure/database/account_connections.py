"""SQLAlchemy persistence for Stage H2 account connections (ADR 0033)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from sqlalchemy import Connection, Engine, insert, select, update
from sqlalchemy.engine import RowMapping

from nervos_core.application.account_connections import (
    AccountConnection,
    ConnectionNotFound,
    InvalidConnection,
    canonical_scopes_json,
    parse_scopes,
)
from nervos_core.application.errors import PersistenceUnavailable
from nervos_core.application.secrets import SecretNotFound
from nervos_core.infrastructure.database.models import AccountConnectionRecord, SecretRecord
from nervos_core.infrastructure.database.transaction import TransactionRunner


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _view(row: AccountConnectionRecord | RowMapping) -> AccountConnection:
    scopes_json = str(row["scopes_json"] if isinstance(row, RowMapping) else row.scopes_json)
    return AccountConnection(
        id=int(row["id"] if isinstance(row, RowMapping) else row.id),
        owner_user_id=int(
            row["owner_user_id"] if isinstance(row, RowMapping) else row.owner_user_id
        ),
        provider=str(row["provider"] if isinstance(row, RowMapping) else row.provider),
        display_name=str(row["display_name"] if isinstance(row, RowMapping) else row.display_name),
        secret_id=int(row["secret_id"] if isinstance(row, RowMapping) else row.secret_id),
        state=str(row["state"] if isinstance(row, RowMapping) else row.state),
        scopes=parse_scopes(scopes_json),
        expires_at=row["expires_at"] if isinstance(row, RowMapping) else row.expires_at,
        created_at=row["created_at"] if isinstance(row, RowMapping) else row.created_at,
        updated_at=row["updated_at"] if isinstance(row, RowMapping) else row.updated_at,
        refresh_revision=int(
            row["refresh_revision"] if isinstance(row, RowMapping) else row.refresh_revision
        ),
        refresh_started_at=row["refresh_started_at"]
        if isinstance(row, RowMapping)
        else row.refresh_started_at,
        revocation_outcome=row["revocation_outcome"]
        if isinstance(row, RowMapping)
        else row.revocation_outcome,
    )


class SqlAlchemyAccountConnectionPersistence:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._transactions = TransactionRunner(engine, _sleep)

    def create_connection(
        self,
        *,
        owner_user_id: int,
        provider: str,
        display_name: str,
        secret_id: int,
        scopes: tuple[str, ...],
        expires_at: datetime | None,
        now: datetime,
    ) -> AccountConnection:
        def write(conn: Connection) -> AccountConnection:
            usable = conn.execute(
                select(SecretRecord.id).where(
                    SecretRecord.id == secret_id,
                    SecretRecord.owner_user_id == owner_user_id,
                    SecretRecord.status == "active",
                )
            ).first()
            if usable is None:
                raise SecretNotFound
            if (
                conn.scalar(
                    select(AccountConnectionRecord.id).where(
                        AccountConnectionRecord.secret_id == secret_id,
                        AccountConnectionRecord.state.in_(("connected", "needs_refresh")),
                    )
                )
                is not None
            ):
                raise InvalidConnection("an active credential already has an account connection")
            cursor = conn.execute(
                insert(AccountConnectionRecord).values(
                    owner_user_id=owner_user_id,
                    provider=provider,
                    display_name=display_name,
                    secret_id=secret_id,
                    state="connected",
                    scopes_json=canonical_scopes_json(scopes),
                    expires_at=expires_at,
                    created_at=now,
                    updated_at=now,
                )
            )
            inserted = cursor.inserted_primary_key
            if inserted is None or inserted[0] is None:  # pragma: no cover - always reported
                raise PersistenceUnavailable
            return _view(self._owned(conn, owner_user_id, int(inserted[0])))

        return self._transactions.run(write)

    def get_connection(self, *, owner_user_id: int, connection_id: int) -> AccountConnection:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    select(AccountConnectionRecord).where(
                        AccountConnectionRecord.id == connection_id,
                        AccountConnectionRecord.owner_user_id == owner_user_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise ConnectionNotFound
        return _view(row)

    def list_connections(self, *, owner_user_id: int) -> tuple[AccountConnection, ...]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    select(AccountConnectionRecord)
                    .where(AccountConnectionRecord.owner_user_id == owner_user_id)
                    .order_by(AccountConnectionRecord.id.desc())
                )
                .mappings()
                .all()
            )
        return tuple(_view(row) for row in rows)

    def set_state(
        self, *, owner_user_id: int, connection_id: int, state: str, now: datetime
    ) -> AccountConnection:
        def write(conn: Connection) -> AccountConnection:
            self._owned(conn, owner_user_id, connection_id)
            conn.execute(
                update(AccountConnectionRecord)
                .where(AccountConnectionRecord.id == connection_id)
                .values(
                    state=state,
                    updated_at=now,
                    refresh_revision=AccountConnectionRecord.refresh_revision + 1,
                    refresh_started_at=None,
                )
            )
            return _view(self._owned(conn, owner_user_id, connection_id))

        return self._transactions.run(write)

    def set_expiry(
        self,
        *,
        owner_user_id: int,
        connection_id: int,
        expires_at: datetime | None,
        now: datetime,
    ) -> AccountConnection:
        def write(conn: Connection) -> AccountConnection:
            self._owned(conn, owner_user_id, connection_id)
            conn.execute(
                update(AccountConnectionRecord)
                .where(AccountConnectionRecord.id == connection_id)
                .values(expires_at=expires_at, updated_at=now)
            )
            return _view(self._owned(conn, owner_user_id, connection_id))

        return self._transactions.run(write)

    @staticmethod
    def _owned(conn: Connection, owner_user_id: int, connection_id: int) -> RowMapping:
        row = (
            conn.execute(
                select(AccountConnectionRecord).where(
                    AccountConnectionRecord.id == connection_id,
                    AccountConnectionRecord.owner_user_id == owner_user_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ConnectionNotFound
        return row

    def claim_refresh(
        self, *, owner_user_id: int, connection_id: int, now: datetime
    ) -> AccountConnection | None:
        def write(conn: Connection) -> AccountConnection | None:
            row = self._owned(conn, owner_user_id, connection_id)
            if row["state"] != "connected" or row["expires_at"] is None or row["expires_at"] > now:
                return None
            conn.execute(
                update(AccountConnectionRecord)
                .where(AccountConnectionRecord.id == connection_id)
                .values(
                    state="needs_refresh",
                    refresh_started_at=now,
                    refresh_revision=int(row["refresh_revision"]) + 1,
                    updated_at=now,
                )
            )
            return _view(self._owned(conn, owner_user_id, connection_id))

        return self._transactions.run(write)

    def finish_refresh(
        self,
        *,
        owner_user_id: int,
        connection_id: int,
        revision: int,
        secret_id: int,
        old_secret_rotation: int,
        expires_at: datetime | None,
        scopes: tuple[str, ...],
        now: datetime,
    ) -> bool:
        def write(conn: Connection) -> bool:
            row = self._owned(conn, owner_user_id, connection_id)
            if (
                row["state"] != "needs_refresh"
                or row["refresh_revision"] != revision
                or row["refresh_started_at"] is None
                or row["refresh_started_at"] + timedelta(seconds=20) <= now
            ):
                return False
            for target, rotation in (
                (int(row["secret_id"]), old_secret_rotation),
                (secret_id, None),
            ):
                metadata = (
                    conn.execute(
                        select(SecretRecord).where(
                            SecretRecord.id == target,
                            SecretRecord.owner_user_id == owner_user_id,
                            SecretRecord.status == "active",
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if metadata is None or (
                    rotation is not None and metadata["rotation_count"] != rotation
                ):
                    return False
            conn.execute(
                update(AccountConnectionRecord)
                .where(AccountConnectionRecord.id == connection_id)
                .values(
                    secret_id=secret_id,
                    state="connected",
                    expires_at=expires_at,
                    scopes_json=canonical_scopes_json(scopes),
                    refresh_started_at=None,
                    refresh_failed_at=None,
                    updated_at=now,
                )
            )
            return True

        return self._transactions.run(write)

    def fail_refresh(
        self, *, owner_user_id: int, connection_id: int, revision: int, now: datetime
    ) -> None:
        def write(conn: Connection) -> None:
            conn.execute(
                update(AccountConnectionRecord)
                .where(
                    AccountConnectionRecord.id == connection_id,
                    AccountConnectionRecord.owner_user_id == owner_user_id,
                    AccountConnectionRecord.state == "needs_refresh",
                    AccountConnectionRecord.refresh_revision == revision,
                )
                .values(refresh_started_at=None, refresh_failed_at=now, updated_at=now)
            )

        self._transactions.run(write)

    def record_revocation(
        self, *, owner_user_id: int, connection_id: int, outcome: str, now: datetime
    ) -> None:
        if outcome not in ("pending", "succeeded", "failed", "not_supported"):
            raise InvalidConnection("revocation outcome is invalid")

        def write(conn: Connection) -> None:
            self._owned(conn, owner_user_id, connection_id)
            conn.execute(
                update(AccountConnectionRecord)
                .where(
                    AccountConnectionRecord.id == connection_id,
                    AccountConnectionRecord.state == "disconnected",
                )
                .values(revocation_outcome=outcome, updated_at=now)
            )

        self._transactions.run(write)
