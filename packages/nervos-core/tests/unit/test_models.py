"""Metadata tests for the Stage A persistence records."""

from nervos_core.infrastructure.database.base import Base
from nervos_core.infrastructure.database.models import AuthSessionRecord, UserRecord
from nervos_core.infrastructure.database.types import UTCDateTime
from sqlalchemy import Boolean, LargeBinary, Table


def test_metadata_contains_exact_application_tables() -> None:
    assert set(Base.metadata.tables) == {"users", "auth_sessions"}


def test_user_metadata_matches_a2_contract() -> None:
    table = UserRecord.__table__
    assert isinstance(table, Table)

    assert list(table.columns.keys()) == [
        "id",
        "username",
        "password_hash",
        "role",
        "is_active",
        "created_at",
        "updated_at",
    ]
    assert table.dialect_options["sqlite"]["autoincrement"] is True
    assert isinstance(table.c.is_active.type, Boolean)
    assert table.c.is_active.server_default is not None
    assert isinstance(table.c.created_at.type, UTCDateTime)
    assert isinstance(table.c.updated_at.type, UTCDateTime)
    assert {constraint.name for constraint in table.constraints} == {
        "pk_users",
        "uq_users_username",
        "ck_users_username_canonical",
        "ck_users_username_length",
        "ck_users_role_nonempty",
        "ck_users_timestamp_order",
    }


def test_auth_session_metadata_matches_a2_contract() -> None:
    table = AuthSessionRecord.__table__
    assert isinstance(table, Table)

    assert list(table.columns.keys()) == [
        "id",
        "user_id",
        "token_hash",
        "created_at",
        "expires_at",
        "revoked_at",
    ]
    assert table.dialect_options["sqlite"]["autoincrement"] is True
    assert isinstance(table.c.token_hash.type, LargeBinary)
    assert table.c.token_hash.type.length == 32
    assert table.c.revoked_at.nullable is True
    assert {constraint.name for constraint in table.constraints} == {
        "pk_auth_sessions",
        "fk_auth_sessions_user_id_users",
        "uq_auth_sessions_token_hash",
        "ck_auth_sessions_expiration_order",
        "ck_auth_sessions_revocation_order",
    }
    assert {index.name for index in table.indexes} == {
        "ix_auth_sessions_user_id",
        "ix_auth_sessions_expires_at",
    }
