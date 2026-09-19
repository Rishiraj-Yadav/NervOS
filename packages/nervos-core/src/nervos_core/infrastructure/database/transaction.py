"""The one short serialized transaction primitive every durable mutation in NervOS runs through.

This module was extracted from the Job persistence module so that more than one persistence module
can share it. The behaviour is unchanged: one `BEGIN IMMEDIATE` transaction per operation, bounded
retry **only** for a failure the driver proved committed nothing, and a connection that is always
*closed* rather than merely rolled back.

Failure classification follows the isolated SQLite validation spike exactly. Only `SQLITE_BUSY` and
`SQLITE_LOCKED` count as provably-uncommitted, and the classification reads the driver's numeric
result code rather than matching on an error string, so a locale or driver change cannot silently
reclassify a fault.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import TypeVar

from sqlalchemy import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from nervos_core.application.errors import PersistenceContention, PersistenceUnavailable

# SQLite primary result codes. Classification uses the driver's own numeric code rather than
# matching on error strings, so a locale or driver change cannot silently reclassify a fault.
_SQLITE_BUSY = 5
_SQLITE_LOCKED = 6
_PRIMARY_RESULT_CODE_MASK = 0xFF

# Bounded retry for a busy/locked transaction that provably committed nothing.
_TRANSACTION_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (0.05, 0.1)

_T = TypeVar("_T")


def is_contention(error: SQLAlchemyError) -> bool:
    """Return whether the driver proved this failure committed nothing.

    Only `SQLITE_BUSY` and `SQLITE_LOCKED` qualify: a busy `BEGIN IMMEDIATE` never opened a
    transaction, and a busy statement or `COMMIT` leaves the transaction open with nothing
    committed. Any other failure is uncertain and is never replayed automatically.
    """
    code = getattr(getattr(error, "orig", None), "sqlite_errorcode", None)
    if not isinstance(code, int):
        return False
    return code & _PRIMARY_RESULT_CODE_MASK in (_SQLITE_BUSY, _SQLITE_LOCKED)


class TransactionRunner:
    """One short `BEGIN IMMEDIATE` transaction per operation, with proven-safe retry only."""

    def __init__(self, engine: Engine, sleep: Callable[[float], None]) -> None:
        self._engine = engine
        self._sleep = sleep

    def run(
        self,
        operation: Callable[[Connection], _T],
        *,
        attempts: int = _TRANSACTION_ATTEMPTS,
    ) -> _T:
        last_error: SQLAlchemyError | None = None
        for index in range(attempts):
            connection = self._engine.connect()
            try:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                result = operation(connection)
                connection.commit()
                return result
            except SQLAlchemyError as error:
                with contextlib.suppress(SQLAlchemyError):
                    connection.rollback()
                if not is_contention(error):
                    raise PersistenceUnavailable from error
                last_error = error
            except BaseException:
                with contextlib.suppress(SQLAlchemyError):
                    connection.rollback()
                raise
            finally:
                # `close()` is the load-bearing step, not `rollback()`. After a failed COMMIT
                # SQLAlchemy no longer believes a transaction is open, so `rollback()` is a
                # no-op while SQLite still holds the lock; returning the connection to the
                # pool performs the real driver-level rollback and releases it. Leaving it
                # open would block every other reader on this database.
                connection.close()
            if index + 1 < attempts:
                self._sleep(_RETRY_BACKOFF_SECONDS[min(index, len(_RETRY_BACKOFF_SECONDS) - 1)])
        # The retry budget is spent and every failure was a busy/locked contention, which
        # proves nothing committed. This is the one failure class a caller may replay.
        raise PersistenceContention from last_error
