"""Stage H5 publisher trust and local revocation enforcement (ADR 0036).

A Stage-G signature is mechanism, not trust. This service turns the fingerprint Stage-G
verification already emits into an explicit, durable, owner-visible local decision and
provides exactly two gates:

* an **install gate** — refuses installation, rebind, or rollback **to** a version signed
  by a ``revoked`` signer. Trusted and untrusted signers install under the existing
  Stage-G authorization flow, which remains the "reviewed decision" mechanism;
* an **execution gate** — denies *new* claim-boundary execution whose pinned installed
  version is bound to a revoked signer.

Neither gate rewrites history: installed software stays installed, and queued/running
Runs keep their immutable snapshots. Revocation bites at the next safe boundary — the
same timing rule every other NervOS security action follows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from nervos_core.application.clock import Clock

TRUST_STATES = ("trusted", "untrusted", "revoked")


class PublisherRevoked(Exception):
    """The signer is locally revoked: the requested install/rebind/execution is refused.

    The public message is a static sentence; it never quotes the reason verbatim back
    into an execution path, and it confirms nothing beyond the refusal itself.
    """

    CODE = "publisher_revoked"
    MESSAGE = "This package's publisher has been revoked locally."


class InvalidTrustState(ValueError):
    """A trust state or reason violates the frozen policy."""


@dataclass(frozen=True, slots=True)
class PublisherTrust:
    """One durable local trust decision."""

    signer_fingerprint: str
    state: str
    decided_by: int
    reason: str | None
    source: str
    created_at: datetime
    updated_at: datetime


class PublisherTrustPersistence(Protocol):
    def set_trust(
        self,
        *,
        signer_fingerprint: str,
        state: str,
        decided_by: int,
        reason: str | None,
        source: str,
        now: datetime,
    ) -> PublisherTrust: ...

    def get_trust(self, signer_fingerprint: str) -> PublisherTrust | None: ...

    def list_trust(self) -> tuple[PublisherTrust, ...]: ...


class PublisherTrustService:
    """Owner-facing trust decisions over the durable store."""

    def __init__(self, persistence: PublisherTrustPersistence, clock: Clock) -> None:
        self._persistence = persistence
        self._clock = clock

    def decide(
        self,
        *,
        signer_fingerprint: str,
        state: str,
        decided_by: int,
        reason: str | None = None,
        source: str = "manual",
    ) -> PublisherTrust:
        if state not in TRUST_STATES:
            raise InvalidTrustState("trust state is invalid")
        return self._persistence.set_trust(
            signer_fingerprint=signer_fingerprint,
            state=state,
            decided_by=decided_by,
            reason=reason,
            source=source,
            now=self._clock(),
        )

    def state_for(self, signer_fingerprint: str) -> str:
        """Return the signer's state; unknown signers are ``untrusted`` by default.

        An unknown signer is not trusted by absence: installation still requires the
        existing explicit Stage-G authorization of the exact artifact.
        """
        record = self._persistence.get_trust(signer_fingerprint)
        return record.state if record is not None else "untrusted"

    def all(self) -> tuple[PublisherTrust, ...]:
        return self._persistence.list_trust()


def ensure_installable(service: PublisherTrustService, signer_fingerprint: str) -> None:
    """Install/rebind/rollback gate: refuse only a revoked signer.

    Trusted and untrusted signers proceed into the unchanged Stage-G installer, which
    still requires explicit operator authorization of the exact version, digest pair,
    and signer evidence. Un-trusting (or never trusting) therefore cannot silently
    disable an already-working review path, and only a deliberate revocation is a wall.
    """
    if service.state_for(signer_fingerprint) == "revoked":
        raise PublisherRevoked


def ensure_execution_admissible(service: PublisherTrustService, signer_fingerprint: str) -> None:
    """New-execution gate: refuse a revoked signer at claim-boundary admission.

    Queued and running Runs accepted before revocation keep their immutable snapshots
    and finish truthfully (ADR 0025). This gate applies to *newly admitted* execution —
    the claim boundary for a package Run — so a revoked signer's package cannot begin
    new work while history remains intact.
    """
    if service.state_for(signer_fingerprint) == "revoked":
        raise PublisherRevoked
