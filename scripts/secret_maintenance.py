"""Explicit local operator maintenance for versioned encrypted secrets.

Never prints values or key material. Does not load .env or migrate a database.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from nervos_core.application.secrets import SecretManager, SecretReferenced, SecretStoreUnavailable
from nervos_core.infrastructure.database import create_sqlite_engine
from nervos_core.infrastructure.database.secrets import SqlAlchemySecretPersistence
from nervos_core.infrastructure.security.secret_keys import FileMasterKeyResolver


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("activate", "reencrypt", "retire"))
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--key-version", required=True, type=int)
    parser.add_argument("--retire-version", type=int)
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error("an existing migrated database is required")
    if args.operation == "retire" and args.retire_version is None:
        parser.error("--retire-version is required for retirement")
    engine = create_sqlite_engine(args.database)
    manager = SecretManager(
        SqlAlchemySecretPersistence(engine),
        FileMasterKeyResolver(args.key_file, args.key_version),
        lambda: datetime.now(UTC),
    )
    try:
        if args.operation == "activate":
            manager.rotate_active_key()
            print("Configured key version activated.")
        elif args.operation == "reencrypt":
            count = manager.reencrypt_batch(args.batch_size)
            print(f"Re-encrypted {count} secrets; repeat until the count is zero.")
        else:
            manager.retire_key_version(args.retire_version)
            print("Key metadata retired. Remove the corresponding historical key file separately.")
    except (SecretReferenced, SecretStoreUnavailable):
        print("Maintenance refused: key unavailable, active or still referenced.")
        return 1
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
