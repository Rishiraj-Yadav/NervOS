"""Focused SQLAlchemy persistence for local authentication."""

from __future__ import annotations

from datetime import datetime
from typing import NoReturn

from sqlalchemy import Select, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from nervos_core.application.authentication import (
    InvalidCredentials,
    PersistenceUnavailable,
    PublicUser,
    SessionCollision,
    SetupComplete,
    StoredCredential,
)
from nervos_core.infrastructure.database.models import AuthSessionRecord, UserRecord


class SqlAlchemyAuthenticationPersistence:
    """Persist users and authentication sessions with explicit transactions."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def any_user_exists(self) -> bool:
        """Return a non-authoritative fast setup-complete hint."""
        try:
            with self._session_factory() as session:
                return session.scalar(select(UserRecord.id).limit(1)) is not None
        except SQLAlchemyError as error:
            raise PersistenceUnavailable from error

    def create_initial_administrator(
        self,
        *,
        username: str,
        password_hash: str,
        token_hash: bytes,
        created_at: datetime,
        expires_at: datetime,
    ) -> PublicUser:
        """Atomically create the first administrator and initial session."""
        self._require_digest(token_hash)
        session = self._session_factory()
        try:
            session.execute(text("BEGIN IMMEDIATE"))
            if session.scalar(select(UserRecord.id).limit(1)) is not None:
                session.rollback()
                raise SetupComplete
            user = UserRecord(
                username=username,
                password_hash=password_hash,
                role="admin",
                is_active=True,
                created_at=created_at,
                updated_at=created_at,
            )
            session.add(user)
            session.flush()
            session.add(
                AuthSessionRecord(
                    user_id=user.id,
                    token_hash=token_hash,
                    created_at=created_at,
                    expires_at=expires_at,
                    revoked_at=None,
                )
            )
            session.commit()
            return self._public_user(user)
        except SetupComplete:
            raise
        except IntegrityError as error:
            session.rollback()
            self._raise_integrity(error)
        except SQLAlchemyError as error:
            session.rollback()
            raise PersistenceUnavailable from error
        finally:
            session.close()

    def get_credential(self, username: str) -> StoredCredential | None:
        """Load one canonical credential without exposing it above the service."""
        try:
            with self._session_factory() as session:
                user = session.scalar(select(UserRecord).where(UserRecord.username == username))
                if user is None:
                    return None
                return StoredCredential(
                    user=self._public_user(user),
                    password_hash=user.password_hash,
                )
        except SQLAlchemyError as error:
            raise PersistenceUnavailable from error

    def create_session(
        self,
        *,
        user_id: int,
        token_hash: bytes,
        created_at: datetime,
        expires_at: datetime,
        replacement_password_hash: str | None,
    ) -> PublicUser:
        """Create a fresh session and optionally replace an obsolete hash."""
        self._require_digest(token_hash)
        try:
            with self._session_factory.begin() as session:
                user = session.get(UserRecord, user_id)
                if user is None or not user.is_active:
                    raise InvalidCredentials
                if replacement_password_hash is not None:
                    user.password_hash = replacement_password_hash
                    user.updated_at = created_at
                session.add(
                    AuthSessionRecord(
                        user_id=user.id,
                        token_hash=token_hash,
                        created_at=created_at,
                        expires_at=expires_at,
                        revoked_at=None,
                    )
                )
                session.flush()
                public_user = self._public_user(user)
            return public_user
        except InvalidCredentials:
            raise
        except IntegrityError as error:
            if "token_hash" in str(error.orig):
                raise SessionCollision from error
            raise PersistenceUnavailable from error
        except SQLAlchemyError as error:
            raise PersistenceUnavailable from error

    def resolve_user(self, token_hash: bytes, now: datetime) -> PublicUser | None:
        """Resolve an unexpired, unrevoked session tied to an active user."""
        self._require_digest(token_hash)
        statement: Select[tuple[UserRecord]] = (
            select(UserRecord)
            .join(AuthSessionRecord, AuthSessionRecord.user_id == UserRecord.id)
            .where(
                AuthSessionRecord.token_hash == token_hash,
                AuthSessionRecord.revoked_at.is_(None),
                AuthSessionRecord.expires_at > now,
                UserRecord.is_active.is_(True),
            )
        )
        try:
            with self._session_factory() as session:
                user = session.scalar(statement)
                return None if user is None else self._public_user(user)
        except SQLAlchemyError as error:
            raise PersistenceUnavailable from error

    def revoke_session(self, token_hash: bytes, revoked_at: datetime) -> None:
        """Idempotently revoke the current matching session."""
        self._require_digest(token_hash)
        try:
            with self._session_factory.begin() as session:
                session.execute(
                    update(AuthSessionRecord)
                    .where(
                        AuthSessionRecord.token_hash == token_hash,
                        AuthSessionRecord.revoked_at.is_(None),
                    )
                    .values(revoked_at=revoked_at)
                )
        except SQLAlchemyError as error:
            raise PersistenceUnavailable from error

    @staticmethod
    def _require_digest(token_hash: bytes) -> None:
        if len(token_hash) != 32:
            raise ValueError("session digest must be exactly 32 bytes")

    @staticmethod
    def _public_user(user: UserRecord) -> PublicUser:
        return PublicUser(
            id=user.id,
            username=user.username,
            role=user.role,
            is_active=user.is_active,
        )

    @staticmethod
    def _raise_integrity(error: IntegrityError) -> NoReturn:
        if "token_hash" in str(error.orig):
            raise SessionCollision from error
        raise PersistenceUnavailable from error
