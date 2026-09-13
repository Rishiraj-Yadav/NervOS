"""Shared application-layer errors."""


class PersistenceUnavailable(Exception):
    """Raised when durable application state cannot be accessed safely."""
