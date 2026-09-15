"""Shared application-layer errors."""


class PersistenceUnavailable(Exception):
    """Raised when durable application state cannot be accessed safely."""


class PersistenceContention(PersistenceUnavailable):
    """Raised when SQLite contention is *proven* to have committed nothing.

    A busy or locked failure is only ever raised as this subclass when the database driver
    itself proves no transaction was left open with a pending commit, or that a failed
    COMMIT left nothing committed. Only then is replaying the whole operation on a fresh
    connection provably safe. Any other persistence failure is the base
    `PersistenceUnavailable`, which is never blindly replayed.
    """


class QueueCapacityExceeded(Exception):
    """Raised when the durable queue is at its configured pending-Job capacity.

    This is an admission decision, never a persistence failure: the two must never be
    conflated in an HTTP response, because a busy database is not a full queue.
    """
