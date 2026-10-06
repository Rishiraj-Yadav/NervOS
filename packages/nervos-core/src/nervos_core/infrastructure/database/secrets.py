"""SQLAlchemy persistence for the encrypted Secret Manager (ADR 0032).

Encryption lives here and nowhere else in the application. Each row stores
``key_version``, a fresh 12-byte nonce, and ``ciphertext||tag``. The GCM additional
authenticated data binds the row identity so ciphertext cannot be swapped between rows or
between versions. Plaintext exists only inside the encrypt/decrypt frames and is never
persisted, logged, or returned by any metadata operation.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import datetime

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import Connection, Engine, delete, insert, select, update
from sqlalchemy.engine import RowMapping

from nervos_core.application.errors import PersistenceUnavailable
from nervos_core.application.secrets import (
    SecretAlreadyExists,
    SecretNotFound,
    SecretReferenced,
    SecretStoreUnavailable,
)
from nervos_core.domain.secrets import (
    MAX_SECRET_VALUE_BYTES,
    InvalidSecret,
    SecretKeyVersion,
    SecretMetadata,
)
from nervos_core.infrastructure.database.models import (
    AccountConnectionRecord,
    SecretKeyVersionRecord,
    SecretRecord,
)
from nervos_core.infrastructure.database.transaction import TransactionRunner

_NONCE_BYTES = 12
# The row id is part of the AAD, so the row has to exist before its ciphertext can be sealed.
# The placeholder is one NUL byte: it satisfies the at-rest invariant (a non-revoked row has
# non-empty ciphertext) for the duration of this single transaction, and the real ciphertext
# replaces it before that transaction commits. A crash between the two statements rolls the
# whole transaction back, so the placeholder is never durable state.
_PLACEHOLDER_CIPHERTEXT = b"\x00"


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _aad(secret_id: int, key_version: int, nonce: bytes) -> bytes:
    return f"{secret_id}:{key_version}:".encode("ascii") + nonce


def _metadata(row: SecretRecord | RowMapping) -> SecretMetadata:
    return SecretMetadata(
        id=int(row["id"] if isinstance(row, RowMapping) else row.id),
        owner_user_id=int(
            row["owner_user_id"] if isinstance(row, RowMapping) else row.owner_user_id
        ),
        name=str(row["name"] if isinstance(row, RowMapping) else row.name),
        provider_hint=(row["provider_hint"] if isinstance(row, RowMapping) else row.provider_hint),
        status=str(row["status"] if isinstance(row, RowMapping) else row.status),
        key_version=int(row["key_version"] if isinstance(row, RowMapping) else row.key_version),
        rotation_count=int(
            row["rotation_count"] if isinstance(row, RowMapping) else row.rotation_count
        ),
        created_at=row["created_at"] if isinstance(row, RowMapping) else row.created_at,
        updated_at=row["updated_at"] if isinstance(row, RowMapping) else row.updated_at,
    )


class SqlAlchemySecretPersistence:
    """The one module that may hold plaintext long enough to encrypt or decrypt it."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._transactions = TransactionRunner(engine, _sleep)

    @staticmethod
    def _cipher(key: bytes) -> AESGCM:
        if len(key) != 32:
            raise SecretStoreUnavailable("the resolved master key has the wrong length")
        return AESGCM(key)

    # -- writes ----------------------------------------------------------------------------------

    def create_secret(
        self,
        *,
        owner_user_id: int,
        name: str,
        plaintext: str,
        provider_hint: str | None,
        key_version: int,
        key: bytes,
        now: datetime,
    ) -> SecretMetadata:
        value_bytes = plaintext.encode("utf-8")
        if not value_bytes or len(value_bytes) > MAX_SECRET_VALUE_BYTES:
            raise InvalidSecret("secret value length is invalid")
        cipher = self._cipher(key)

        def write(conn: Connection) -> SecretMetadata:
            cursor = conn.execute(
                insert(SecretRecord).values(
                    owner_user_id=owner_user_id,
                    name=name,
                    provider_hint=provider_hint,
                    status="active",
                    key_version=key_version,
                    nonce=b"",
                    ciphertext=_PLACEHOLDER_CIPHERTEXT,
                    rotation_count=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            inserted = cursor.inserted_primary_key
            if inserted is None or inserted[0] is None:  # pragma: no cover - always reported
                raise PersistenceUnavailable
            secret_id = int(inserted[0])
            nonce = os.urandom(_NONCE_BYTES)
            sealed = cipher.encrypt(nonce, value_bytes, _aad(secret_id, key_version, nonce))
            conn.execute(
                update(SecretRecord)
                .where(SecretRecord.id == secret_id)
                .values(nonce=nonce, ciphertext=sealed)
            )
            row = (
                conn.execute(select(SecretRecord).where(SecretRecord.id == secret_id))
                .mappings()
                .one()
            )
            return _metadata(row)

        try:
            return self._transactions.run(write)
        except SecretAlreadyExists:
            raise
        except Exception as error:
            if "owner_secret_name" in str(error):
                raise SecretAlreadyExists from error
            raise

    def replace_secret(
        self,
        *,
        owner_user_id: int,
        secret_id: int,
        plaintext: str,
        key_version: int,
        key: bytes,
        now: datetime,
    ) -> SecretMetadata:
        value_bytes = plaintext.encode("utf-8")
        if not value_bytes or len(value_bytes) > MAX_SECRET_VALUE_BYTES:
            raise InvalidSecret("secret value length is invalid")
        cipher = self._cipher(key)

        def write(conn: Connection) -> SecretMetadata:
            row = (
                conn.execute(
                    select(SecretRecord).where(
                        SecretRecord.id == secret_id,
                        SecretRecord.owner_user_id == owner_user_id,
                        SecretRecord.status != "revoked",
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise SecretNotFound
            rotation_count = int(row["rotation_count"])
            nonce = os.urandom(_NONCE_BYTES)
            sealed = cipher.encrypt(nonce, value_bytes, _aad(secret_id, key_version, nonce))
            conn.execute(
                update(SecretRecord)
                .where(SecretRecord.id == secret_id)
                .values(
                    nonce=nonce,
                    ciphertext=sealed,
                    key_version=key_version,
                    rotation_count=rotation_count + 1,
                    updated_at=now,
                )
            )
            refreshed = (
                conn.execute(select(SecretRecord).where(SecretRecord.id == secret_id))
                .mappings()
                .one()
            )
            return _metadata(refreshed)

        return self._transactions.run(write)

    def set_secret_status(
        self, *, owner_user_id: int, secret_id: int, status: str, now: datetime
    ) -> SecretMetadata:
        if status not in ("active", "disabled", "revoked"):
            raise InvalidSecret("secret status is invalid")

        def write(conn: Connection) -> SecretMetadata:
            row = (
                conn.execute(
                    select(SecretRecord).where(
                        SecretRecord.id == secret_id,
                        SecretRecord.owner_user_id == owner_user_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise SecretNotFound
            if row["status"] == "revoked" and status != "revoked":
                raise InvalidSecret("revocation is terminal")
            referenced = (
                conn.execute(
                    select(AccountConnectionRecord.id).where(
                        AccountConnectionRecord.secret_id == secret_id
                    )
                ).first()
                is not None
            )
            if referenced and status in ("disabled", "revoked"):
                raise SecretReferenced
            values: dict[str, object] = {"status": status, "updated_at": now}
            if status == "revoked":
                # Terminal: destroy the recoverable material, keep the row as evidence.
                values["nonce"] = b""
                values["ciphertext"] = b""
            conn.execute(update(SecretRecord).where(SecretRecord.id == secret_id).values(**values))
            refreshed = (
                conn.execute(select(SecretRecord).where(SecretRecord.id == secret_id))
                .mappings()
                .one()
            )
            return _metadata(refreshed)

        return self._transactions.run(write)

    def delete_secret(self, *, owner_user_id: int, secret_id: int) -> None:
        def write(conn: Connection) -> None:
            referenced = (
                conn.execute(
                    select(AccountConnectionRecord.id).where(
                        AccountConnectionRecord.secret_id == secret_id
                    )
                ).first()
                is not None
            )
            if referenced:
                raise SecretReferenced
            result = conn.execute(
                delete(SecretRecord).where(
                    SecretRecord.id == secret_id,
                    SecretRecord.owner_user_id == owner_user_id,
                )
            )
            if result.rowcount == 0:
                raise SecretNotFound

        self._transactions.run(write)

    # -- reads -----------------------------------------------------------------------------------

    def get_secret(self, *, owner_user_id: int, secret_id: int) -> SecretMetadata:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    select(SecretRecord).where(
                        SecretRecord.id == secret_id,
                        SecretRecord.owner_user_id == owner_user_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise SecretNotFound
        return _metadata(row)

    def list_secrets(self, *, owner_user_id: int) -> tuple[SecretMetadata, ...]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    select(SecretRecord)
                    .where(SecretRecord.owner_user_id == owner_user_id)
                    .order_by(SecretRecord.id.desc())
                )
                .mappings()
                .all()
            )
        return tuple(_metadata(row) for row in rows)

    def decrypt_active_value(
        self, *, owner_user_id: int, secret_id: int, resolve_key: Callable[[int], bytes]
    ) -> str:
        """Resolve one owned, active secret's plaintext; every other shape fails closed."""
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    select(SecretRecord).where(
                        SecretRecord.id == secret_id,
                        SecretRecord.owner_user_id == owner_user_id,
                        SecretRecord.status == "active",
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise SecretNotFound
        ciphertext = bytes(row["ciphertext"])
        nonce = bytes(row["nonce"])
        if not ciphertext:
            raise SecretNotFound
        try:
            value = self._cipher(resolve_key(int(row["key_version"]))).decrypt(
                nonce,
                ciphertext,
                _aad(int(row["id"]), int(row["key_version"]), nonce),
            )
        except Exception as error:
            raise SecretStoreUnavailable("the stored secret could not be authenticated") from error
        return value.decode("utf-8")

    # -- key versions ----------------------------------------------------------------------------

    def list_key_versions(self) -> tuple[SecretKeyVersion, ...]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(SecretKeyVersionRecord).order_by(SecretKeyVersionRecord.key_version)
            ).all()
        return tuple(
            SecretKeyVersion(
                key_version=int(row.key_version), active=bool(row.active), created_at=row.created_at
            )
            for row in rows
        )

    def activate_key_version(self, *, key_version: int, now: datetime) -> SecretKeyVersion:
        def write(conn: Connection) -> SecretKeyVersion:
            conn.execute(update(SecretKeyVersionRecord).values(active=False))
            existing = conn.execute(
                select(SecretKeyVersionRecord).where(
                    SecretKeyVersionRecord.key_version == key_version
                )
            ).one_or_none()
            if existing is None:
                conn.execute(
                    insert(SecretKeyVersionRecord).values(
                        key_version=key_version, active=True, created_at=now
                    )
                )
            else:
                conn.execute(
                    update(SecretKeyVersionRecord)
                    .where(SecretKeyVersionRecord.key_version == key_version)
                    .values(active=True)
                )
            row = conn.execute(
                select(SecretKeyVersionRecord).where(
                    SecretKeyVersionRecord.key_version == key_version
                )
            ).one()
            return SecretKeyVersion(
                key_version=int(row.key_version), active=bool(row.active), created_at=row.created_at
            )

        return self._transactions.run(write)

    def reencrypt_batch(
        self,
        *,
        key_version: int,
        key: bytes,
        resolve_key: Callable[[int], bytes],
        limit: int,
        now: datetime,
    ) -> int:
        cipher = self._cipher(key)

        def write(conn: Connection) -> int:
            rows = (
                conn.execute(
                    select(SecretRecord)
                    .where(
                        SecretRecord.key_version != key_version,
                        SecretRecord.status != "revoked",
                    )
                    .order_by(SecretRecord.id)
                    .limit(limit)
                )
                .mappings()
                .all()
            )
            for row in rows:
                secret_id = int(row["id"])
                old_version = int(row["key_version"])
                old_nonce = bytes(row["nonce"])
                try:
                    plaintext = self._cipher(resolve_key(old_version)).decrypt(
                        old_nonce, bytes(row["ciphertext"]), _aad(secret_id, old_version, old_nonce)
                    )
                except Exception as error:
                    raise SecretStoreUnavailable(
                        "maintenance could not authenticate a secret"
                    ) from error
                nonce = os.urandom(_NONCE_BYTES)
                sealed = cipher.encrypt(nonce, plaintext, _aad(secret_id, key_version, nonce))
                conn.execute(
                    update(SecretRecord)
                    .where(SecretRecord.id == secret_id)
                    .values(
                        key_version=key_version,
                        nonce=nonce,
                        ciphertext=sealed,
                        rotation_count=int(row["rotation_count"]) + 1,
                        updated_at=now,
                    )
                )
            return len(rows)

        return self._transactions.run(write)

    def retire_key_version(self, *, key_version: int) -> None:
        def write(conn: Connection) -> None:
            referenced = conn.execute(
                select(SecretRecord.id)
                .where(SecretRecord.key_version == key_version, SecretRecord.status != "revoked")
                .limit(1)
            ).first()
            active = conn.execute(
                select(SecretKeyVersionRecord.key_version).where(
                    SecretKeyVersionRecord.key_version == key_version,
                    SecretKeyVersionRecord.active.is_(True),
                )
            ).first()
            if referenced is not None or active is not None:
                raise SecretReferenced("the key version remains active or referenced")
            conn.execute(
                delete(SecretKeyVersionRecord).where(
                    SecretKeyVersionRecord.key_version == key_version
                )
            )

        self._transactions.run(write)
