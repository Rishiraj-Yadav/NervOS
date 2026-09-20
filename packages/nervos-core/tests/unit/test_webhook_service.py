"""Stage E3 webhook application values, provisioning, rotation, and the ingress ordering.

The service is exercised against recording fakes rather than a database, because what is under test
here is the *order* of its decisions — which is the security property — and an order is easier to
assert against a list of calls than against durable side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from nervos_core.application.agent_definitions import (
    AgentDefinitionResolver,
    UnknownAgentDefinition,
)
from nervos_core.application.errors import (
    PersistenceContention,
    PersistenceUnavailable,
    QueueCapacityExceeded,
)
from nervos_core.application.triggers import (
    ResolvedAgentDefinition,
    TriggerDraft,
    TriggerNotEditable,
    TriggerNotFound,
)
from nervos_core.application.webhooks import (
    PUBLIC_ID_ALLOCATION_ATTEMPTS,
    IssuedSecret,
    IssuedWebhook,
    WebhookDefinitionDrifted,
    WebhookDelivery,
    WebhookDeliveryCommand,
    WebhookDeliveryKind,
    WebhookDeliveryResult,
    WebhookDeliveryService,
    WebhookProvisioningService,
    WebhookPublicIdConflict,
    WebhookSecretService,
    WebhookSecretVerifier,
)
from nervos_core.domain.agents import AgentDefinition, AgentDefinitionId
from nervos_core.domain.runs import TOOL_ENABLED_LIMITS
from nervos_core.domain.triggers import (
    InvalidTrigger,
    MisfirePolicy,
    OccurrenceStatus,
    TriggerDefinition,
    TriggerKind,
    TriggerOccurrence,
)
from nervos_core.domain.webhooks import WebhookPayloadRejection, parse_webhook_payload
from nervos_core.infrastructure.security.webhook_secrets import (
    DUMMY_DIGEST,
    digest_secret,
    generate_public_id,
    generate_secret,
    secret_matches,
)
from nervos_core.infrastructure.webhooks import HashingWebhookSecretVerifier

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
DEFINITION_ID = AgentDefinitionId("nervos.chat", "2")
RESOLVED = ResolvedAgentDefinition(definition_id=DEFINITION_ID, limits=TOOL_ENABLED_LIMITS)
SECRET = generate_secret()


def webhook_definition(
    *,
    secret_digest: bytes | None = None,
    enabled: bool = True,
    config_revision: int = 1,
    public_id: str | None = None,
) -> TriggerDefinition:
    return TriggerDefinition(
        id=1,
        owner_user_id=1,
        agent_instance_id=1,
        kind=TriggerKind.WEBHOOK,
        display_name="On delivery",
        enabled=enabled,
        input_text="handle the delivery",
        config_revision=config_revision,
        misfire_policy=MisfirePolicy.COALESCE_ONE,
        next_fire_at=None,
        run_at=None,
        interval_seconds=None,
        cron_expression=None,
        timezone=None,
        public_id=public_id or generate_public_id(),
        created_at=NOW,
        updated_at=NOW,
        secret_digest=secret_digest if secret_digest is not None else digest_secret(SECRET),
        secret_created_at=NOW,
        event_type=None,
        agent_definition_id=DEFINITION_ID,
    )


def occurrence(*, status: OccurrenceStatus, run_id: int | None, skip_code: str | None = None):
    skipped = status is OccurrenceStatus.SKIPPED
    return TriggerOccurrence(
        id=41,
        trigger_definition_id=1,
        owner_user_id=1,
        agent_instance_id=1,
        trigger_revision=1,
        status=status,
        run_id=run_id,
        skip_code=skip_code if skipped else None,
        skip_message="static" if skipped else None,
        nominal_at=None,
        event_id=None,
        idempotency_key="key-1",
        occurred_at=NOW,
        created_at=NOW,
    )


def delivery(**overrides: object) -> WebhookDelivery:
    defaults: dict[str, object] = {
        "public_id": generate_public_id(),
        "candidate_secret": SECRET,
        "idempotency_key": None,
        "has_conflicting_idempotency_keys": False,
        "is_json_content_type": True,
        "occurred_at": NOW,
    }
    return WebhookDelivery(**{**defaults, **overrides})  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------------
# Result invariants
# ------------------------------------------------------------------------------------------------


def test_an_applied_outcome_carries_an_occurrence() -> None:
    created = occurrence(status=OccurrenceStatus.RUN_CREATED, run_id=7)
    result = WebhookDeliveryResult(kind=WebhookDeliveryKind.MATERIALIZED, occurrence=created)

    assert result.occurrence_id == 41
    assert result.run_id == 7
    assert result.code is None


def test_a_skip_publishes_only_a_frozen_public_code() -> None:
    skipped = occurrence(status=OccurrenceStatus.SKIPPED, run_id=None, skip_code="agent_disabled")
    result = WebhookDeliveryResult(kind=WebhookDeliveryKind.SKIPPED, occurrence=skipped)
    assert result.code == "agent_disabled"

    other = occurrence(status=OccurrenceStatus.SKIPPED, run_id=None, skip_code="schedule_invalid")
    leaked = WebhookDeliveryResult(kind=WebhookDeliveryKind.SKIPPED, occurrence=other)
    assert leaked.code is None


def test_an_outcome_that_wrote_nothing_carries_no_occurrence() -> None:
    for kind in (
        WebhookDeliveryKind.UNAUTHENTICATED,
        WebhookDeliveryKind.DISABLED,
        WebhookDeliveryKind.CONFLICT,
        WebhookDeliveryKind.CAPACITY_EXCEEDED,
        WebhookDeliveryKind.TEMPORARILY_UNAVAILABLE,
        WebhookDeliveryKind.MALFORMED_PAYLOAD,
        WebhookDeliveryKind.INVALID_IDEMPOTENCY_KEY,
        WebhookDeliveryKind.UNSUPPORTED_MEDIA_TYPE,
    ):
        result = WebhookDeliveryResult(kind=kind)
        assert result.occurrence_id is None
        assert result.code is None

    with pytest.raises(InvalidTrigger):
        WebhookDeliveryResult(
            kind=WebhookDeliveryKind.CONFLICT,
            occurrence=occurrence(status=OccurrenceStatus.RUN_CREATED, run_id=1),
        )


def test_only_a_duplicate_is_marked_as_one() -> None:
    created = occurrence(status=OccurrenceStatus.RUN_CREATED, run_id=1)
    with pytest.raises(InvalidTrigger):
        WebhookDeliveryResult(
            kind=WebhookDeliveryKind.MATERIALIZED, occurrence=created, duplicate=True
        )


def test_an_applied_outcome_requires_its_occurrence() -> None:
    with pytest.raises(InvalidTrigger):
        WebhookDeliveryResult(kind=WebhookDeliveryKind.MATERIALIZED)


# ------------------------------------------------------------------------------------------------
# Value-object safety
# ------------------------------------------------------------------------------------------------


def test_a_plaintext_secret_never_reaches_a_repr() -> None:
    issued = IssuedWebhook(trigger=webhook_definition(), secret="super-secret-value")
    secret = IssuedSecret(digest=DUMMY_DIGEST, secret="super-secret-value")

    assert "super-secret-value" not in repr(issued)
    assert "super-secret-value" not in repr(secret)
    assert issued.secret == "super-secret-value"


def a_payload(raw: bytes = b'{"a": 1}'):
    parsed = parse_webhook_payload(raw)
    assert not isinstance(parsed, WebhookPayloadRejection), parsed
    return parsed


def test_a_command_refuses_an_unusable_shape() -> None:
    payload = a_payload()

    with pytest.raises(InvalidTrigger):
        WebhookDeliveryCommand(
            trigger_definition_id=0,
            candidate_secret_digest=DUMMY_DIGEST,
            idempotency_key=None,
            payload=payload,
            definition=RESOLVED,
            occurred_at=NOW,
        )

    with pytest.raises(InvalidTrigger):
        WebhookDeliveryCommand(
            trigger_definition_id=1,
            candidate_secret_digest=DUMMY_DIGEST,
            idempotency_key=None,
            payload=payload,
            definition=RESOLVED,
            occurred_at=datetime(2026, 9, 20, 12, 0),
        )


def test_a_delivery_requires_an_aware_instant() -> None:
    with pytest.raises(InvalidTrigger):
        delivery(occurred_at=datetime(2026, 9, 20, 12, 0))


# ------------------------------------------------------------------------------------------------
# Provisioning
# ------------------------------------------------------------------------------------------------


@dataclass
class RecordingCredentialPersistence:
    conflicts: int = 0
    created: list[TriggerDraft] = field(default_factory=list[TriggerDraft])
    rotated: tuple[int, int, bytes, datetime] | None = None
    kind: TriggerKind = TriggerKind.WEBHOOK

    def create_webhook_trigger(
        self, owner_user_id: int, draft: TriggerDraft, now: datetime
    ) -> TriggerDefinition:
        self.created.append(draft)
        if self.conflicts > 0:
            self.conflicts -= 1
            raise WebhookPublicIdConflict
        return webhook_definition()

    def rotate_webhook_secret(
        self, owner_user_id: int, trigger_id: int, secret_digest: bytes, now: datetime
    ) -> TriggerDefinition:
        if self.kind is not TriggerKind.WEBHOOK:
            raise TriggerNotEditable("only a webhook trigger carries a secret")
        self.rotated = (owner_user_id, trigger_id, secret_digest, now)
        return webhook_definition(secret_digest=secret_digest)


class CountingFactory:
    def __init__(self) -> None:
        self.locators: list[str] = []
        self.secrets: list[str] = []

    def new_public_id(self) -> str:
        value = generate_public_id()
        self.locators.append(value)
        return value

    def new_secret(self) -> IssuedSecret:
        value = generate_secret()
        self.secrets.append(value)
        return IssuedSecret(digest=digest_secret(value), secret=value)


def provisioning(persistence: RecordingCredentialPersistence, factory: CountingFactory):
    return WebhookProvisioningService(persistence, factory)


def test_provisioning_issues_a_locator_and_a_credential_once() -> None:
    persistence = RecordingCredentialPersistence()
    factory = CountingFactory()
    issued = provisioning(persistence, factory).create_webhook_trigger(
        owner_user_id=1,
        agent_instance_id=1,
        display_name="On delivery",
        input_text="handle it",
        now=NOW,
    )

    draft = persistence.created[0]
    assert issued.secret == factory.secrets[0]
    assert draft.public_id == factory.locators[0]  # type: ignore[attr-defined]
    assert draft.secret_digest == digest_secret(issued.secret)  # type: ignore[attr-defined]
    assert draft.kind is TriggerKind.WEBHOOK  # type: ignore[attr-defined]
    assert secret_matches(issued.secret, draft.secret_digest)  # type: ignore[attr-defined]


def test_provisioning_retries_a_collision_with_a_fresh_locator_and_secret() -> None:
    persistence = RecordingCredentialPersistence(conflicts=1)
    factory = CountingFactory()
    issued = provisioning(persistence, factory).create_webhook_trigger(
        owner_user_id=1,
        agent_instance_id=1,
        display_name="On delivery",
        input_text="handle it",
        now=NOW,
    )

    assert len(persistence.created) == 2
    assert factory.locators[0] != factory.locators[1]
    assert factory.secrets[0] != factory.secrets[1]
    # The credential that survived is the second one, and the first was never persisted.
    assert issued.secret == factory.secrets[1]
    assert issued.secret != factory.secrets[0]


def test_provisioning_gives_up_bounded_when_every_locator_is_taken() -> None:
    persistence = RecordingCredentialPersistence(conflicts=PUBLIC_ID_ALLOCATION_ATTEMPTS)
    with pytest.raises(PersistenceUnavailable):
        provisioning(persistence, CountingFactory()).create_webhook_trigger(
            owner_user_id=1,
            agent_instance_id=1,
            display_name="On delivery",
            input_text="handle it",
            now=NOW,
        )
    assert len(persistence.created) == PUBLIC_ID_ALLOCATION_ATTEMPTS


# ------------------------------------------------------------------------------------------------
# Rotation
# ------------------------------------------------------------------------------------------------


def test_rotation_issues_a_new_credential_and_leaves_the_locator_alone() -> None:
    persistence = RecordingCredentialPersistence()
    factory = CountingFactory()
    issued = WebhookSecretService(persistence, factory).rotate_secret(
        owner_user_id=1, trigger_id=1, now=NOW
    )

    owner, trigger_id, digest, moment = persistence.rotated  # type: ignore[attr-defined]
    assert (owner, trigger_id, moment) == (1, 1, NOW)
    assert digest == digest_secret(issued.secret)
    assert issued.secret == factory.secrets[0]
    assert factory.locators == []


# ------------------------------------------------------------------------------------------------
# The ingress ordering
# ------------------------------------------------------------------------------------------------


@dataclass
class RecordingIngressPersistence:
    trigger: TriggerDefinition | None = None
    result: WebhookDeliveryResult | None = None
    raises: BaseException | None = None
    commands: list[WebhookDeliveryCommand] = field(default_factory=list[WebhookDeliveryCommand])
    lookups: list[str] = field(default_factory=list[str])

    def find_webhook_by_public_id(self, public_id: str) -> TriggerDefinition | None:
        self.lookups.append(public_id)
        return self.trigger

    def materialize_webhook_delivery(self, command: WebhookDeliveryCommand):
        self.commands.append(command)
        if self.raises is not None:
            raise self.raises
        assert self.result is not None
        return self.result


class StubResolver:
    """Resolves to one pinned definition, or refuses -- the two states the ingress distinguishes."""

    def __init__(self, resolved: ResolvedAgentDefinition | None = RESOLVED) -> None:
        self._resolved = resolved

    def resolve(self, definition_id: AgentDefinitionId) -> AgentDefinition:
        if self._resolved is None:
            raise UnknownAgentDefinition
        return AgentDefinition(
            identity=self._resolved.definition_id,
            display_name="Nervos Chat",
            limits=self._resolved.limits,
        )


def build_service(
    persistence: RecordingIngressPersistence,
    *,
    verifier: WebhookSecretVerifier | None = None,
    resolver: AgentDefinitionResolver | None = None,
) -> WebhookDeliveryService:
    return WebhookDeliveryService(
        persistence,
        resolver if resolver is not None else StubResolver(),
        verifier if verifier is not None else HashingWebhookSecretVerifier(),
    )


BODY = b'{"event": "created"}'


def test_an_unknown_locator_is_refused_before_the_body_is_looked_at() -> None:
    persistence = RecordingIngressPersistence(trigger=None)
    result = build_service(persistence).receive(delivery(), b"not json at all")

    assert result.kind is WebhookDeliveryKind.UNAUTHENTICATED
    assert persistence.commands == []


def test_a_wrong_secret_is_refused_before_the_body_is_looked_at() -> None:
    persistence = RecordingIngressPersistence(trigger=webhook_definition())
    result = build_service(persistence).receive(delivery(candidate_secret=generate_secret()), b"{")

    assert result.kind is WebhookDeliveryKind.UNAUTHENTICATED
    assert persistence.commands == []


def test_a_non_json_content_type_is_refused_only_after_authentication() -> None:
    persistence = RecordingIngressPersistence(trigger=webhook_definition())
    service = build_service(persistence)

    refused = service.receive(delivery(is_json_content_type=False), BODY)
    assert refused.kind is WebhookDeliveryKind.UNSUPPORTED_MEDIA_TYPE

    # The same request without a credential gets the authentication answer instead, so a caller
    # who cannot authenticate never learns whether their content type would have been acceptable.
    unauthenticated = service.receive(
        delivery(is_json_content_type=False, candidate_secret=None), BODY
    )
    assert unauthenticated.kind is WebhookDeliveryKind.UNAUTHENTICATED


def test_a_malformed_body_is_refused_only_after_authentication() -> None:
    persistence = RecordingIngressPersistence(trigger=webhook_definition())
    service = build_service(persistence)

    assert service.receive(delivery(), b"{not json").kind is WebhookDeliveryKind.MALFORMED_PAYLOAD
    assert (
        service.receive(delivery(candidate_secret=None), b"{not json").kind
        is WebhookDeliveryKind.UNAUTHENTICATED
    )


def test_conflicting_idempotency_keys_are_refused() -> None:
    persistence = RecordingIngressPersistence(trigger=webhook_definition())
    result = build_service(persistence).receive(
        delivery(idempotency_key="a", has_conflicting_idempotency_keys=True), BODY
    )
    assert result.kind is WebhookDeliveryKind.INVALID_IDEMPOTENCY_KEY
    assert persistence.commands == []


def test_a_malformed_key_is_refused_before_any_write() -> None:
    persistence = RecordingIngressPersistence(trigger=webhook_definition())
    result = build_service(persistence).receive(delivery(idempotency_key="has space"), BODY)
    assert result.kind is WebhookDeliveryKind.INVALID_IDEMPOTENCY_KEY
    assert persistence.commands == []


def test_an_unresolvable_definition_refuses_retryably() -> None:
    persistence = RecordingIngressPersistence(trigger=webhook_definition())
    result = build_service(persistence, resolver=StubResolver(None)).receive(delivery(), BODY)
    assert result.kind is WebhookDeliveryKind.TEMPORARILY_UNAVAILABLE
    assert persistence.commands == []


def test_the_command_carries_a_digest_and_never_the_plaintext() -> None:
    persistence = RecordingIngressPersistence(
        trigger=webhook_definition(),
        result=WebhookDeliveryResult(
            kind=WebhookDeliveryKind.MATERIALIZED,
            occurrence=occurrence(status=OccurrenceStatus.RUN_CREATED, run_id=9),
        ),
    )
    build_service(persistence).receive(delivery(idempotency_key="key-1"), BODY)

    command = persistence.commands[0]
    assert command.candidate_secret_digest == digest_secret(SECRET)
    assert command.idempotency_key == "key-1"
    assert command.payload.byte_count == len(BODY)
    assert SECRET not in repr(command)


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (QueueCapacityExceeded(), WebhookDeliveryKind.CAPACITY_EXCEEDED),
        (WebhookDefinitionDrifted(), WebhookDeliveryKind.TEMPORARILY_UNAVAILABLE),
        (PersistenceContention(), WebhookDeliveryKind.TEMPORARILY_UNAVAILABLE),
        (PersistenceUnavailable(), WebhookDeliveryKind.TEMPORARILY_UNAVAILABLE),
        (TriggerNotFound(1), WebhookDeliveryKind.UNAUTHENTICATED),
    ],
)
def test_a_transaction_failure_maps_to_its_frozen_outcome(
    raised: BaseException, expected: WebhookDeliveryKind
) -> None:
    persistence = RecordingIngressPersistence(trigger=webhook_definition(), raises=raised)
    assert build_service(persistence).receive(delivery(), BODY).kind is expected
