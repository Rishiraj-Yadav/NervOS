"""Durable owner-bound, single-use OAuth state; PKCE material is a secret reference."""

from __future__ import annotations

import time
from datetime import datetime

from sqlalchemy import Connection, Engine, insert, select, update

from nervos_core.application.account_connections import canonical_scopes_json, parse_scopes
from nervos_core.application.account_oauth import (
    AccountAuthorizationUnavailable,
    AccountOAuthRequest,
)
from nervos_core.infrastructure.database.models import AccountOAuthRequestRecord
from nervos_core.infrastructure.database.transaction import TransactionRunner


class SqlAlchemyAccountOAuthPersistence:
    def __init__(self, engine: Engine) -> None:
        self._transactions = TransactionRunner(engine, time.sleep)

    def create(
        self, *, state_hash: str, request: AccountOAuthRequest, expires_at: datetime, now: datetime
    ) -> None:
        def write(conn: Connection) -> None:
            conn.execute(
                insert(AccountOAuthRequestRecord).values(
                    state_hash=state_hash,
                    owner_user_id=request.owner_user_id,
                    provider=request.provider,
                    secret_id=request.secret_id,
                    scopes_json=canonical_scopes_json(request.scopes),
                    display_name=request.display_name,
                    expires_at=expires_at,
                    created_at=now,
                )
            )

        self._transactions.run(write)

    def claim(self, *, owner_user_id: int, state_hash: str, now: datetime) -> AccountOAuthRequest:
        def write(conn: Connection) -> AccountOAuthRequest:
            row = (
                conn.execute(
                    select(AccountOAuthRequestRecord).where(
                        AccountOAuthRequestRecord.state_hash == state_hash,
                        AccountOAuthRequestRecord.owner_user_id == owner_user_id,
                        AccountOAuthRequestRecord.used_at.is_(None),
                        AccountOAuthRequestRecord.expires_at > now,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise AccountAuthorizationUnavailable
            conn.execute(
                update(AccountOAuthRequestRecord)
                .where(AccountOAuthRequestRecord.state_hash == state_hash)
                .values(used_at=now)
            )
            return AccountOAuthRequest(
                owner_user_id,
                str(row["provider"]),
                int(row["secret_id"]),
                parse_scopes(str(row["scopes_json"])),
                str(row["display_name"]),
            )

        return self._transactions.run(write)
