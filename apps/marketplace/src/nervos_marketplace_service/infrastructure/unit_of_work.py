"""Bounded parameterized writes. No public route receives this unit of work."""

from collections.abc import Generator
from contextlib import contextmanager
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from nervos_marketplace_service.domain.errors import MarketplaceError
from nervos_marketplace_service.domain.identity import Record, now

# Closed schema/column allowlist. Identifiers never originate in HTTP input.
TABLES: dict[str, frozenset[str]] = {
    "verification_admission": frozenset(["id", "operation_id", "generation", "expires_at"]),
    "marketplace_accounts": frozenset(
        [
            "id",
            "state",
            "created_at",
        ]
    ),
    "external_identities": frozenset(
        [
            "id",
            "account_id",
            "issuer",
            "subject",
            "created_at",
        ]
    ),
    "publishers": frozenset(
        ["id", "handle", "display_name", "kind", "state", "revision", "created_at", "updated_at"]
    ),
    "publisher_memberships": frozenset(
        ["publisher_id", "account_id", "role", "state", "created_at", "updated_at"]
    ),
    "marketplace_operator_grants": frozenset(
        [
            "account_id",
            "active",
            "created_at",
        ]
    ),
    "hosted_sessions": frozenset(
        [
            "token_hash",
            "account_id",
            "authenticated_at",
            "acr",
            "amr",
            "csrf_hash",
            "created_at",
            "expires_at",
            "idle_expires_at",
            "revoked",
        ]
    ),
    "cli_credentials": frozenset(
        [
            "token_hash",
            "account_id",
            "authenticated_at",
            "acr",
            "amr",
            "scope",
            "publisher_id",
            "created_at",
            "expires_at",
            "revoked",
        ]
    ),
    "auth_transactions": frozenset(
        [
            "state_hash",
            "browser_hash",
            "nonce_hash",
            "encrypted_verifier",
            "redirect_uri",
            "cli_state",
            "cli_challenge",
            "scope",
            "publisher_id",
            "completed_account_id",
            "expires_at",
            "consumed",
            "consented",
        ]
    ),
    "authorization_codes": frozenset(
        [
            "code_hash",
            "account_id",
            "client_id",
            "redirect_uri",
            "challenge",
            "scope",
            "publisher_id",
            "authenticated_at",
            "acr",
            "amr",
            "expires_at",
            "consumed",
        ]
    ),
    "publisher_signing_keys": frozenset(
        ["id", "publisher_id", "public_key", "fingerprint", "state", "created_at", "updated_at"]
    ),
    "key_proof_challenges": frozenset(
        ["id", "publisher_id", "account_id", "public_key", "payload", "expires_at", "consumed"]
    ),
    "package_projects": frozenset(
        ["id", "package_id", "publisher_id", "ownership_revision", "visibility", "created_at"]
    ),
    "project_key_authorizations": frozenset(
        ["project_id", "key_id", "ownership_revision", "state", "actor_id", "created_at"]
    ),
    "project_transfers": frozenset(
        [
            "id",
            "project_id",
            "source_publisher_id",
            "destination_publisher_id",
            "ownership_revision",
            "requester_id",
            "accepting_id",
            "state",
            "created_at",
            "expires_at",
        ]
    ),
    "package_listings": frozenset(
        ["project_id", "display_name", "summary", "description", "revision", "updated_at"]
    ),
    "artifacts": frozenset(
        [
            "archive_sha256",
            "size_bytes",
            "storage_version_id",
            "created_at",
        ]
    ),
    "package_releases": frozenset(
        [
            "id",
            "project_id",
            "original_publisher_id",
            "exact_version",
            "archive_sha256",
            "content_digest",
            "signer_fingerprint",
            "manifest_version",
            "manifest_bytes",
            "runtime_language",
            "runtime_python",
            "nervos_min_version",
            "nervos_max_version",
            "semver_key",
            "is_prerelease",
            "publication_state",
            "distribution_state",
            "status_revision",
            "published_at",
            "status_updated_at",
        ]
    ),
    "upload_operations": frozenset(
        [
            "id",
            "actor_id",
            "publisher_id",
            "project_id",
            "ownership_revision",
            "archive_sha256",
            "size_bytes",
            "quarantine_key",
            "state",
            "failure_code",
            "attempt_count",
            "generation",
            "lease_token",
            "lease_expires_at",
            "upload_token",
            "upload_expires_at",
            "ready_release_id",
            "created_at",
            "updated_at",
            "expires_at",
            "credential_hash",
        ]
    ),
    "publication_idempotency": frozenset(
        ["actor_id", "action", "target", "key", "request_digest", "result_id", "expires_at"]
    ),
    "release_status_advisories": frozenset(
        [
            "release_id",
            "revision",
            "state",
            "reason",
            "created_at",
        ]
    ),
}


def columns(table: str, values: Record) -> None:
    if table not in TABLES or not values.keys() <= TABLES[table]:
        raise ValueError("Unknown hosted table/column")


def parameters(values: Record) -> Record:
    import json

    return {
        key: json.dumps(value) if key in {"amr", "evidence"} else value
        for key, value in values.items()
    }


class PostgresTransaction:
    def __init__(self, connection: Connection, request_id: str) -> None:
        self.connection = connection
        self.request_id = request_id

    def _select(self, table: str, where: Record, limit: int, lock: bool) -> list[Record]:
        columns(table, where)
        if not where or not 1 <= limit <= 100:
            raise ValueError("Bounded scoped query required")
        clause = " AND ".join(f"{key}=:{key}" for key in where)
        query = f"SELECT * FROM {table} WHERE {clause} LIMIT :row_limit"
        if lock:
            query += " FOR UPDATE"
        return [
            dict(row)
            for row in self.connection.execute(
                text(query), {**where, "row_limit": limit}
            ).mappings()
        ]

    def get(self, table: str, where: Record, *, lock: bool = True) -> Record:
        rows = self._select(table, where, 1, lock)
        if not rows:
            raise MarketplaceError("not_found", 404)
        return rows[0]

    def find(self, table: str, where: Record, *, limit: int = 100) -> list[Record]:
        return self._select(table, where, limit, False)

    def insert(self, table: str, values: Record) -> None:
        columns(table, values)
        keys = ",".join(values)
        markers = ",".join(f":{key}" for key in values)
        self.connection.execute(
            text(f"INSERT INTO {table} ({keys}) VALUES ({markers})"), parameters(values)
        )

    def update(self, table: str, where: Record, values: Record) -> None:
        columns(table, where)
        columns(table, values)
        if not where:
            raise ValueError("Unscoped mutation prohibited")
        sets = ",".join(f"{key}=:set_{key}" for key in values)
        clause = " AND ".join(f"{key}=:{key}" for key in where)
        self.connection.execute(
            text(f"UPDATE {table} SET {sets} WHERE {clause}"),
            {**where, **{f"set_{k}": v for k, v in parameters(values).items()}},
        )

    def audit(self, actor: UUID | None, action: str, target: str, evidence: Record) -> None:
        import json

        self.connection.execute(
            text("""INSERT INTO publication_audit_events
          (id,actor_kind,actor_id,action,target,request_id,occurred_at,evidence)
          VALUES(:id,:kind,:actor,:action,:target,:request,:at,CAST(:evidence AS jsonb))"""),
            {
                "id": uuid4(),
                "kind": "human" if actor else "system",
                "actor": actor,
                "action": action,
                "target": target,
                "request": self.request_id[:64],
                "at": now(),
                "evidence": json.dumps(evidence, default=str, allow_nan=False),
            },
        )

    def rate_limit(self, account_id: UUID, action: str, maximum: int) -> None:
        # Caller locks the account; concurrent quota checks for that account serialize.
        count = self.connection.execute(
            text("""SELECT count(*) FROM publication_audit_events
          WHERE actor_id=:actor AND action=:action AND occurred_at>:after"""),
            {"actor": account_id, "action": action, "after": now() - timedelta(minutes=1)},
        ).scalar_one()
        if count >= maximum:
            raise MarketplaceError("rate_limited", 429)

    def upload_capacity(self, account_id: UUID, publisher_id: UUID) -> tuple[int, int, int]:
        row = self.connection.execute(
            text("""SELECT
          (SELECT count(*) FROM upload_operations WHERE actor_id=:actor
            AND state IN ('uploading','uploaded','verifying') AND expires_at>:at),
          (SELECT count(*) FROM upload_operations WHERE publisher_id=:publisher
            AND state IN ('uploading','uploaded','verifying') AND expires_at>:at),
          (SELECT coalesce(sum(size_bytes),0) FROM upload_operations WHERE actor_id=:actor
            AND created_at>:after)"""),
            {
                "actor": account_id,
                "publisher": publisher_id,
                "at": now(),
                "after": now() - timedelta(days=1),
            },
        ).one()
        return int(row[0]), int(row[1]), int(row[2])


class PostgresUnitOfWork:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @contextmanager
    def transaction(self, request_id: str = "system") -> Generator[PostgresTransaction]:
        try:
            with self.engine.begin() as connection:
                yield PostgresTransaction(connection, request_id)
        except IntegrityError:
            raise MarketplaceError("ownership_conflict", 409) from None
        except SQLAlchemyError:
            raise MarketplaceError("service_unavailable", 503) from None
