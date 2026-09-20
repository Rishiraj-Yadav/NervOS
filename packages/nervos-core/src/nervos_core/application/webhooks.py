"""Stage E3 webhook ingress: delivery orchestration, credential provisioning, and rotation.

This module is provider-neutral and transport-neutral. It carries no FastAPI type, no Starlette
type, no SQLAlchemy type and no database handle, so an alternative persistence implementation, a
test double or a second ingress can satisfy these ports without knowing what any of those are.

The one ordering decision worth reading twice is here, and it is **deliberately the inverse of
E2's**. A schedule resolves identity *before* authority, because a duplicate decision describes work
already done and must not become an error for whichever of two racing schedulers arrived second. A
webhook delivery authenticates *before* it looks up a duplicate identity, because an invalidated
secret that could still retrieve an existing occurrence's outcome would be releasing a result to a
credential that no longer holds any authority. Both orderings are correct for their own subject;
neither may be copied onto the other.

Everything authoritative happens in the transaction the persistence port owns: the trigger re-read,
the current-secret comparison, the enabled check, the identity lookup, the Run and Job insertion and
the occurrence write. This module decides nothing durable by itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from nervos_core.application.agent_definitions import (
    AgentDefinitionResolver,
    UnknownAgentDefinition,
)
from nervos_core.application.clock import require_utc
from nervos_core.application.errors import (
    PersistenceContention,
    PersistenceUnavailable,
    QueueCapacityExceeded,
)
from nervos_core.application.triggers import (
    ResolvedAgentDefinition,
    TriggerDraft,
    TriggerNotFound,
    resolve_agent_definition,
)
from nervos_core.domain.triggers import (
    InvalidTrigger,
    OccurrenceStatus,
    TriggerDefinition,
    TriggerKind,
    TriggerOccurrence,
)
from nervos_core.domain.webhooks import (
    PUBLIC_WEBHOOK_SKIP_CODES,
    WebhookPayload,
    WebhookPayloadRejection,
    is_well_formed_idempotency_key,
    parse_webhook_payload,
)

#: How many times provisioning will try to allocate an unused locator before giving up. A
#: `public_id` is ~128 random bits, so a second attempt is already astronomically unlikely; the
#: bound exists so that a pathological collision cannot become an unbounded loop, and a fresh
#: locator **and** a fresh secret are generated for every attempt so no attempt can reuse one.
PUBLIC_ID_ALLOCATION_ATTEMPTS = 3


class WebhookPublicIdConflict(ValueError):
    """The generated locator is already taken. Raised by persistence, never parsed out of a message.

    This exists so provisioning can retry a collision **without** reading a driver's exception text
    or matching a constraint name. A typed signal is the only kind of signal that may influence a
    retry; string inspection of an error message is exactly the fragile classification this
    repository refuses.
    """


class WebhookDefinitionDrifted(ValueError):
    """The target Agent Instance no longer resolves to the Agent Definition that was resolved.

    A typed signal for the same reason: the webhook answer to drift is a retryable refusal that
    consumes nothing, and that decision must not depend on matching an error's wording.
    """


class WebhookDeliveryKind(StrEnum):
    """What one delivery attempt produced. Never persisted.

    Only three of these correspond to a durable row -- ``MATERIALIZED``, ``SKIPPED`` and
    ``DUPLICATE`` write or return an occurrence. Every other member means nothing was recorded, and
    exists so the ingress can say what it refused without inventing history to say it with.
    """

    MATERIALIZED = "materialized"
    SKIPPED = "skipped"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"
    UNAUTHENTICATED = "unauthenticated"
    DISABLED = "disabled"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    MALFORMED_PAYLOAD = "malformed_payload"
    INVALID_IDEMPOTENCY_KEY = "invalid_idempotency_key"
    CAPACITY_EXCEEDED = "capacity_exceeded"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"


#: The kinds that carry an occurrence. The complement wrote nothing at all.
_KINDS_WITH_AN_OCCURRENCE = (
    WebhookDeliveryKind.MATERIALIZED,
    WebhookDeliveryKind.SKIPPED,
    WebhookDeliveryKind.DUPLICATE,
)

#: The only reason a delivery refreshes an existing result rather than creating one.
_DUPLICATE_ONLY = (WebhookDeliveryKind.DUPLICATE,)


@dataclass(frozen=True, slots=True)
class WebhookDelivery:
    """One delivery's transport-neutral facts, produced by the ingress boundary.

    ``candidate_secret`` is the credential the caller presented, already stripped of its scheme by
    the HTTP layer -- which is the last place that knows an `Authorization` header exists. It is
    never persisted and never logged. ``is_json_content_type`` is carried as a *fact* rather than
    acted on at the boundary, because the frozen ordering refuses a non-JSON body only **after**
    authentication succeeds.

    ``has_conflicting_idempotency_keys`` records that the caller supplied more than one value for
    one header. That is an ambiguity, not a choice: the ingress refuses it rather than silently
    picking one, so only a single, well-formed key can ever become a durable identity.
    """

    public_id: str
    candidate_secret: str | None
    idempotency_key: str | None
    has_conflicting_idempotency_keys: bool
    is_json_content_type: bool
    occurred_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", _aware(self.occurred_at))


@dataclass(frozen=True, slots=True)
class WebhookDeliveryCommand:
    """Everything one webhook materialization must verify or write.

    Deliberately **absent**: owner, target Agent Instance, trigger revision and the model-visible
    instruction. All four are re-read from the durable row inside the transaction, so they have
    exactly one authority -- and the operator's instruction in particular can never be supplied by
    a caller.

    ``candidate_secret_digest`` is a **digest**, never the plaintext secret, so the plaintext never
    crosses this boundary and cannot reach a traceback from inside the transaction. The `payload`
    is the one thing specific to this delivery that durable state cannot supply, which is why it --
    and not the composed instruction -- is what travels here: the operator's half of the composed
    input is re-read from the durable row, so a caller can never supply it.
    """

    trigger_definition_id: int
    candidate_secret_digest: bytes
    idempotency_key: str | None
    payload: WebhookPayload
    definition: ResolvedAgentDefinition
    occurred_at: datetime

    def __post_init__(self) -> None:
        if self.trigger_definition_id <= 0:
            raise InvalidTrigger("invalid trigger identifier")
        object.__setattr__(self, "occurred_at", _aware(self.occurred_at))


@dataclass(frozen=True, slots=True)
class WebhookDeliveryResult:
    """What one delivery attempt produced, in the vocabulary the ingress counts."""

    kind: WebhookDeliveryKind
    occurrence: TriggerOccurrence | None = None
    duplicate: bool = False

    def __post_init__(self) -> None:
        if self.kind in _KINDS_WITH_AN_OCCURRENCE:
            if self.occurrence is None:
                raise InvalidTrigger("an applied outcome carries an occurrence")
        elif self.occurrence is not None:
            raise InvalidTrigger("an outcome that wrote nothing carries no occurrence")
        if self.duplicate and self.kind not in _DUPLICATE_ONLY:
            raise InvalidTrigger("only a duplicate outcome is marked as one")

    @property
    def occurrence_id(self) -> int | None:
        occurrence = self.occurrence
        return None if occurrence is None else occurrence.id

    @property
    def run_id(self) -> int | None:
        occurrence = self.occurrence
        return None if occurrence is None else occurrence.run_id

    @property
    def code(self) -> str | None:
        """The public skip code, or ``None``. Never an internal reason.

        Only the two frozen webhook-reachable skip values are ever published; anything else is
        reported as absent rather than leaked by accident.
        """
        occurrence = self.occurrence
        if occurrence is None or occurrence.status is OccurrenceStatus.RUN_CREATED:
            return None
        skip_code = occurrence.skip_code
        return skip_code if skip_code in PUBLIC_WEBHOOK_SKIP_CODES else None


class WebhookSecretVerifier(Protocol):
    """Verifies a presented credential against a stored digest.

    Two methods, because the ingress needs both: the pre-transaction check answers "may this caller
    proceed at all?", and the digest is what the transaction re-checks against the **current**
    stored value. Neither ever sees a plaintext secret leave this process.
    """

    def verify(self, candidate: str | None, stored_digest: bytes | None) -> bool:
        """Whether the credential matches. Always performs a comparison, found or not."""
        ...

    def digest(self, candidate: str | None) -> bytes:
        """The fixed-width digest of a candidate, dummy-substituted when it is unusable."""
        ...


class WebhookIngressPersistence(Protocol):
    """What the ingress needs from durable state: locate, then materialize."""

    def find_webhook_by_public_id(self, public_id: str) -> TriggerDefinition | None:
        """The webhook trigger a locator names, or ``None``. A non-webhook row is not a match."""
        ...

    def materialize_webhook_delivery(
        self, command: WebhookDeliveryCommand
    ) -> WebhookDeliveryResult:
        """Verify one delivery against durable state and apply it, or write nothing.

        One transaction contains the trigger re-read, the current-secret comparison, the
        existing-identity lookup, the enabled check, the Agent/definition check, the input
        composition, the canonical Run and Job insertion and the occurrence write -- in the frozen
        order, which authenticates **before** it looks up an identity.
        """
        ...


class WebhookCredentialFactory(Protocol):
    """Issues locators and secrets. Kept separate so no security policy lives in the ingress."""

    def new_public_id(self) -> str: ...

    def new_secret(self) -> IssuedSecret: ...


@dataclass(frozen=True, slots=True)
class IssuedSecret:
    """A newly generated secret and its digest, produced exactly once."""

    digest: bytes
    #: Present in this value and nowhere else, ever. `repr=False` so it cannot reach a log or a
    #: traceback through a dataclass repr -- the same reasoning `TriggerDefinition.secret_digest`
    #: already carries.
    secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class IssuedWebhook:
    """A webhook trigger together with the credential that was issued for it, exactly once."""

    trigger: TriggerDefinition
    secret: str = field(repr=False)


class WebhookCredentialPersistence(Protocol):
    """Provisioning and rotation, the two operations that write credential material."""

    def create_webhook_trigger(
        self, owner_user_id: int, draft: TriggerDraft, now: datetime
    ) -> TriggerDefinition:
        """Create a webhook trigger, refusing a taken locator with `WebhookPublicIdConflict`."""
        ...

    def rotate_webhook_secret(
        self, owner_user_id: int, trigger_id: int, secret_digest: bytes, now: datetime
    ) -> TriggerDefinition: ...


class WebhookDeliveryService:
    """Order one delivery through the frozen sequence and hand it to atomic persistence.

    It owns **ordering and resolution only**: it reads nothing from the database itself beyond the
    locator lookup the frozen pre-transaction step requires, holds no connection, and makes no
    authority decision. The one authority decision -- "is the presented credential still the current
    one?" -- is made again inside the transaction, which is the only place it counts.
    """

    def __init__(
        self,
        persistence: WebhookIngressPersistence,
        definitions: AgentDefinitionResolver,
        verifier: WebhookSecretVerifier,
    ) -> None:
        self._persistence = persistence
        self._definitions = definitions
        self._verifier = verifier

    def receive(self, delivery: WebhookDelivery, raw_body: bytes) -> WebhookDeliveryResult:
        """The whole decision sequence, in the one order that is correct for a webhook.

        The credential is settled **before** the body is given any meaning: a caller who cannot
        authenticate learns nothing about whether their JSON would have been accepted, and no
        parsing work is spent on their behalf. The body has already been bounded by the transport
        layer, which happens earlier still.
        """
        trigger = self._persistence.find_webhook_by_public_id(delivery.public_id)
        stored_digest = None if trigger is None else trigger.secret_digest
        # The comparison always runs: an unknown locator still costs a digest comparison, so the
        # two paths do the same work.
        matched = self._verifier.verify(delivery.candidate_secret, stored_digest)
        if trigger is None or not matched:
            return WebhookDeliveryResult(kind=WebhookDeliveryKind.UNAUTHENTICATED)

        if not delivery.is_json_content_type:
            return WebhookDeliveryResult(kind=WebhookDeliveryKind.UNSUPPORTED_MEDIA_TYPE)

        payload = parse_webhook_payload(raw_body)
        if isinstance(payload, WebhookPayloadRejection):
            return WebhookDeliveryResult(kind=WebhookDeliveryKind.MALFORMED_PAYLOAD)

        idempotency_key = delivery.idempotency_key
        if delivery.has_conflicting_idempotency_keys:
            return WebhookDeliveryResult(kind=WebhookDeliveryKind.INVALID_IDEMPOTENCY_KEY)
        if idempotency_key is not None and not is_well_formed_idempotency_key(idempotency_key):
            return WebhookDeliveryResult(kind=WebhookDeliveryKind.INVALID_IDEMPOTENCY_KEY)

        resolved = self._resolve(trigger)
        if resolved is None:
            # The target Agent Definition is no longer resolvable. Nothing is consumed: the sender
            # may retry, and a retry re-resolves whatever is current then.
            return WebhookDeliveryResult(kind=WebhookDeliveryKind.TEMPORARILY_UNAVAILABLE)

        command = WebhookDeliveryCommand(
            trigger_definition_id=trigger.id,
            candidate_secret_digest=self._verifier.digest(delivery.candidate_secret),
            idempotency_key=idempotency_key,
            payload=payload,
            definition=resolved,
            occurred_at=delivery.occurred_at,
        )
        return self._materialize(command)

    def _resolve(self, trigger: TriggerDefinition) -> ResolvedAgentDefinition | None:
        try:
            return resolve_agent_definition(self._definitions, trigger)
        except (InvalidTrigger, UnknownAgentDefinition):
            return None

    def _materialize(self, command: WebhookDeliveryCommand) -> WebhookDeliveryResult:
        try:
            return self._persistence.materialize_webhook_delivery(command)
        except TriggerNotFound:
            # The trigger was deleted between the locator lookup and the transaction. The endpoint
            # no longer exists, so the answer is the same one an unknown locator gets -- and the
            # caller learns nothing beyond that.
            return WebhookDeliveryResult(kind=WebhookDeliveryKind.UNAUTHENTICATED)
        except QueueCapacityExceeded:
            # Admission backpressure is not a refusal to record: the whole attempt rolled back, so
            # the delivery is neither lost nor consumed, and the sender may safely retry -- with the
            # same idempotency key, because nothing was committed against it.
            return WebhookDeliveryResult(kind=WebhookDeliveryKind.CAPACITY_EXCEEDED)
        except WebhookDefinitionDrifted:
            return WebhookDeliveryResult(kind=WebhookDeliveryKind.TEMPORARILY_UNAVAILABLE)
        except (PersistenceContention, PersistenceUnavailable):
            return WebhookDeliveryResult(kind=WebhookDeliveryKind.TEMPORARILY_UNAVAILABLE)


class WebhookProvisioningService:
    """Create a webhook trigger and issue its credential exactly once.

    It owns no cryptography: locators and secrets come from an injected factory, so the security
    primitives have one home and this service is about the *policy* -- generate, persist, return the
    plaintext once, and never let a locator collision become an unbounded loop or a raw 500.
    """

    def __init__(
        self,
        persistence: WebhookCredentialPersistence,
        factory: WebhookCredentialFactory,
        *,
        attempts: int = PUBLIC_ID_ALLOCATION_ATTEMPTS,
    ) -> None:
        self._persistence = persistence
        self._factory = factory
        self._attempts = attempts

    def create_webhook_trigger(
        self,
        *,
        owner_user_id: int,
        agent_instance_id: int,
        display_name: str,
        input_text: str,
        now: datetime,
    ) -> IssuedWebhook:
        """Create one webhook trigger, retrying a locator collision a bounded number of times."""
        for _ in range(self._attempts):
            issued = self._factory.new_secret()
            draft = TriggerDraft(
                agent_instance_id=agent_instance_id,
                display_name=display_name,
                input_text=input_text,
                kind=TriggerKind.WEBHOOK,
                public_id=self._factory.new_public_id(),
                secret_digest=issued.digest,
                secret_created_at=now,
            )
            try:
                trigger = self._persistence.create_webhook_trigger(owner_user_id, draft, now)
            except WebhookPublicIdConflict:
                # A fresh locator *and* a fresh secret are generated for the next attempt, so no
                # attempt can inherit the previous one's credential.
                continue
            return IssuedWebhook(trigger=trigger, secret=issued.secret)
        raise PersistenceUnavailable("could not allocate a webhook locator")


class WebhookSecretService:
    """Replace a webhook trigger's credential, atomically and without changing its locator.

    Rotation does **not** increment `config_revision`. The repository defines a defining change as
    one that alters "the configuration that determines what fires"; a credential determines *who
    may ask*, not *what fires*, and `secret_created_at` exists as the purpose-built record of when
    it was replaced. The locator is deliberately unchanged, so an endpoint URL survives a rotation
    and only the credential moves.
    """

    def __init__(
        self, persistence: WebhookCredentialPersistence, factory: WebhookCredentialFactory
    ) -> None:
        self._persistence = persistence
        self._factory = factory

    def rotate_secret(self, *, owner_user_id: int, trigger_id: int, now: datetime) -> IssuedWebhook:
        issued = self._factory.new_secret()
        trigger = self._persistence.rotate_webhook_secret(
            owner_user_id, trigger_id, issued.digest, now
        )
        return IssuedWebhook(trigger=trigger, secret=issued.secret)


def _aware(value: datetime) -> datetime:
    """Reuse the application clock boundary's rule, reported in this module's vocabulary."""
    try:
        return require_utc(value)
    except ValueError as error:
        raise InvalidTrigger("a webhook instant must be timezone-aware") from error


__all__ = [
    "PUBLIC_ID_ALLOCATION_ATTEMPTS",
    "IssuedSecret",
    "IssuedWebhook",
    "WebhookCredentialFactory",
    "WebhookCredentialPersistence",
    "WebhookDefinitionDrifted",
    "WebhookDelivery",
    "WebhookDeliveryCommand",
    "WebhookDeliveryKind",
    "WebhookDeliveryResult",
    "WebhookDeliveryService",
    "WebhookIngressPersistence",
    "WebhookProvisioningService",
    "WebhookPublicIdConflict",
    "WebhookSecretService",
    "WebhookSecretVerifier",
]
