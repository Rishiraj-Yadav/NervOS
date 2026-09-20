"""The webhook credential adapters: one verifier, one factory.

Two bindings of application ports to the Stage E1 security primitives, in the same shape
`infrastructure/scheduling.py` uses for the schedule evaluator: the port is defined where it is
used, the implementation lives here, and a composition root chooses it. That is what keeps
`hmac`, `hashlib` and `secrets` out of the application layer while leaving the application free to
decide *when* verification happens.
"""

from __future__ import annotations

from nervos_core.application.webhooks import (
    IssuedSecret,
    WebhookCredentialFactory,
    WebhookSecretVerifier,
)
from nervos_core.infrastructure.security.webhook_secrets import (
    digest_secret,
    generate_public_id,
    generate_secret,
    secret_digest_for,
    secret_matches,
)


class HashingWebhookSecretVerifier:
    """`WebhookSecretVerifier` over the frozen SHA-256 credential primitives.

    It adds no policy of its own: whether a dummy may authorize is decided by `secret_matches`,
    which is the one place that knows whether a real stored digest existed.
    """

    def verify(self, candidate: str | None, stored_digest: bytes | None) -> bool:
        return secret_matches(candidate, stored_digest)

    def digest(self, candidate: str | None) -> bytes:
        return secret_digest_for(candidate)


class RandomWebhookCredentialFactory:
    """`WebhookCredentialFactory` over `secrets.token_urlsafe`.

    A locator and a secret are generated separately and neither is derived from the other, so the
    endpoint identity -- which is visible in every request line -- is never also the credential.
    """

    def new_public_id(self) -> str:
        return generate_public_id()

    def new_secret(self) -> IssuedSecret:
        secret = generate_secret()
        return IssuedSecret(digest=digest_secret(secret), secret=secret)


def create_webhook_secret_verifier() -> WebhookSecretVerifier:
    """The verifier this process should use. One construction, one home for the comparison."""
    return HashingWebhookSecretVerifier()


def create_webhook_credential_factory() -> WebhookCredentialFactory:
    """The credential factory this process should use."""
    return RandomWebhookCredentialFactory()


__all__ = [
    "HashingWebhookSecretVerifier",
    "RandomWebhookCredentialFactory",
    "create_webhook_credential_factory",
    "create_webhook_secret_verifier",
]
