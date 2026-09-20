"""The Stage E3 webhook harness.

One migrated temporary database, one composed ingress, and the smallest set of helpers a delivery
journey needs. It deliberately builds on the E2 scheduler harness rather than reimplementing it:
`migrate` already creates the disposable database, applies every migration, and seeds the owners and
Agent Instances a trigger needs, so the webhook suite inherits exactly the same world the scheduler
suite runs against.

Everything here is deterministic. The clock is a parameter, never a wait, and the races are modelled
with barriers rather than sleeps.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nervos_core.application.agent_definitions import (
    AgentDefinitionResolver,
    create_builtin_definition_registry,
)
from nervos_core.application.triggers import ResolvedAgentDefinition
from nervos_core.application.webhooks import (
    IssuedWebhook,
    WebhookDelivery,
    WebhookDeliveryResult,
    WebhookDeliveryService,
    WebhookProvisioningService,
    WebhookSecretService,
)
from nervos_core.domain.agents import AgentDefinition, AgentDefinitionId
from nervos_core.domain.runs import TOOL_ENABLED_LIMITS
from nervos_core.infrastructure.database.jobs import SqlAlchemyJobPersistence
from nervos_core.infrastructure.database.models import TriggerDefinitionRecord
from nervos_core.infrastructure.database.triggers import SqlAlchemyTriggerPersistence
from nervos_core.infrastructure.webhooks import (
    HashingWebhookSecretVerifier,
    RandomWebhookCredentialFactory,
)
from scheduler_support import AGENT, NOW, OTHER_OWNER, OWNER, migrate
from sqlalchemy import Engine, select, text

#: A deterministic body. Two deliveries carrying these exact bytes are the same delivery only when
#: they also carry the same idempotency key; without one they are two distinct events.
DEFAULT_BODY = b'{"event": "created", "id": 7}'

#: The definition identity every seeded Agent Instance carries, matching the scheduler harness.
DEFINITION_ID = AgentDefinitionId("nervos.chat", "2")


#: Distinguishes "use the credential that was issued" from "present no credential at all". A plain
#: `None` default could not express the second, and a missing credential is a case the ingress is
#: required to refuse.
_USE_ISSUED: Any = object()


@dataclass(slots=True)
class WebhookRig:
    """A composed ingress over one disposable database."""

    engine: Engine
    path: Path
    triggers: SqlAlchemyTriggerPersistence
    provisioning: WebhookProvisioningService
    secrets: WebhookSecretService
    service: WebhookDeliveryService
    rotation_sequence: list[str] = field(default_factory=list[str])

    # ------------------------------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------------------------------

    def provision(
        self,
        *,
        owner: int = OWNER,
        agent_instance_id: int = AGENT,
        display_name: str = "On delivery",
        input_text: str = "handle the delivery",
        now: datetime = NOW,
    ) -> IssuedWebhook:
        """Create a webhook trigger and return its credential exactly once."""
        return self.provisioning.create_webhook_trigger(
            owner_user_id=owner,
            agent_instance_id=agent_instance_id,
            display_name=display_name,
            input_text=input_text,
            now=now,
        )

    def rotate(self, trigger_id: int, *, owner: int = OWNER, now: datetime = NOW) -> IssuedWebhook:
        issued = self.secrets.rotate_secret(owner_user_id=owner, trigger_id=trigger_id, now=now)
        self.rotation_sequence.append(issued.secret)
        return issued

    def set_enabled(self, trigger_id: int, enabled: bool, *, owner: int = OWNER) -> None:
        self.triggers.set_enabled(owner, trigger_id, enabled, NOW)

    def set_agent_enabled(self, enabled: bool) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("UPDATE agent_instances SET enabled = :enabled WHERE id = :id"),
                {"enabled": 1 if enabled else 0, "id": AGENT},
            )

    def set_agent_definition(self, version: str) -> None:
        """Retarget the Agent Instance, so a resolved definition no longer matches the row."""
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE agent_instances SET agent_definition_version = :version WHERE id = :id"
                ),
                {"version": version, "id": AGENT},
            )

    # ------------------------------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------------------------------

    def deliver(
        self,
        issued: IssuedWebhook,
        *,
        body: bytes = DEFAULT_BODY,
        secret: Any = _USE_ISSUED,
        public_id: str | None = None,
        idempotency_key: str | None = None,
        conflicting_keys: bool = False,
        json_content_type: bool = True,
        occurred_at: datetime = NOW,
    ) -> WebhookDeliveryResult:
        """Deliver one body to one trigger, through the composed ingress."""
        candidate = issued.secret if secret is _USE_ISSUED else secret
        return self.service.receive(
            WebhookDelivery(
                public_id=public_id if public_id is not None else issued.trigger.public_id or "",
                candidate_secret=candidate,
                idempotency_key=idempotency_key,
                has_conflicting_idempotency_keys=conflicting_keys,
                is_json_content_type=json_content_type,
                occurred_at=occurred_at,
            ),
            body,
        )

    # ------------------------------------------------------------------------------------------
    # Durable truth
    # ------------------------------------------------------------------------------------------

    def counts(self) -> dict[str, int]:
        with self.engine.connect() as connection:
            return {
                table: int(connection.scalar(text(f"SELECT COUNT(*) FROM {table}")) or 0)
                for table in (
                    "runs",
                    "jobs",
                    "job_attempts",
                    "run_events",
                    "trigger_occurrences",
                )
            }

    def occurrences(self, trigger_id: int) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT id, status, skip_code, run_id, idempotency_key, "
                        "payload_digest, payload_bytes, trigger_revision "
                        "FROM trigger_occurrences WHERE trigger_definition_id = :trigger "
                        "ORDER BY id"
                    ),
                    {"trigger": trigger_id},
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    def run_row(self, run_id: int) -> dict[str, Any]:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT id, status, input_text, agent_key, agent_definition_version, "
                        "model_provider, model_name FROM runs WHERE id = :id"
                    ),
                    {"id": run_id},
                )
                .mappings()
                .one()
            )
        return dict(row)

    def job_rows(self, run_id: int) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    text("SELECT id, status, run_id FROM jobs WHERE run_id = :id ORDER BY id"),
                    {"id": run_id},
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    def stored_secret_created_at(self, trigger_id: int) -> object:
        with self.engine.connect() as connection:
            return connection.scalar(
                select(TriggerDefinitionRecord.secret_created_at).where(
                    TriggerDefinitionRecord.id == trigger_id
                )
            )

    def stored_config_revision(self, trigger_id: int) -> int:
        with self.engine.connect() as connection:
            revision = connection.scalar(
                select(TriggerDefinitionRecord.config_revision).where(
                    TriggerDefinitionRecord.id == trigger_id
                )
            )
        assert revision is not None
        return int(revision)

    def fill_queue(self, *, runs: int = 1) -> None:
        """Admit manual Runs until the webhook's own cap is reached.

        A separate persistence is used for the filler, so the runs occupying the slots are
        admitted at a cap the test controls rather than through the cap under test.
        """
        filler = SqlAlchemyJobPersistence(self.engine, max_pending=1000)
        for _ in range(runs):
            filler.submit(
                owner_user_id=OWNER,
                agent_instance_id=AGENT,
                input_text="occupy a slot",
                limits=TOOL_ENABLED_LIMITS,
                definition_id=DEFINITION_ID,
                now=NOW,
            )


class StaleDefinitionResolver:
    """
    Resolve every definition identity to one fixed identity, whatever is asked for.

    The drift race is otherwise unreproducible: retargeting an Agent Instance to another
    *registered* definition would resolve differently but legitimately. Pinning the resolved
    identity is what models "the value read before the transaction no longer matches the row".
    """

    def __init__(self, pinned: AgentDefinitionId) -> None:
        self._pinned = pinned

    def resolve(self, definition_id: AgentDefinitionId) -> AgentDefinition:
        del definition_id
        return AgentDefinition(
            identity=self._pinned, display_name="NervosOS Chat", limits=TOOL_ENABLED_LIMITS
        )


def resolved(pinned: AgentDefinitionId = DEFINITION_ID) -> ResolvedAgentDefinition:
    return ResolvedAgentDefinition(definition_id=pinned, limits=TOOL_ENABLED_LIMITS)


def build_service(
    persistence: SqlAlchemyTriggerPersistence,
    *,
    resolver: AgentDefinitionResolver | None = None,
) -> WebhookDeliveryService:
    """Compose the ingress over a persistence, defaulting to the trusted built-in registry."""
    return WebhookDeliveryService(
        persistence,
        resolver if resolver is not None else create_builtin_definition_registry(),
        HashingWebhookSecretVerifier(),
    )


def build_rig(
    path: Path,
    monkeypatch: Any,
    *,
    agents: int = 1,
    max_pending: int = 1000,
    resolver: AgentDefinitionResolver | None = None,
) -> WebhookRig:
    """Compose one ingress over a freshly migrated disposable database."""
    engine = migrate(path, monkeypatch, agents=agents, max_pending=max_pending)
    triggers = SqlAlchemyTriggerPersistence(engine, max_pending=max_pending, sleep=lambda _: None)
    factory = RandomWebhookCredentialFactory()
    return WebhookRig(
        engine=engine,
        path=path,
        triggers=triggers,
        provisioning=WebhookProvisioningService(triggers, factory),
        secrets=WebhookSecretService(triggers, factory),
        service=build_service(triggers, resolver=resolver),
    )


class Racer:
    """Run two callables on their own database connections, released together by a barrier.

    Each thread opens its own engine on the same file, which is what makes this a real race between
    two transactions rather than two calls sharing one connection. The barrier is what makes it
    deterministic: neither side starts before the other is ready, and SQLite's write lock -- not a
    sleep -- decides who wins.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def race(self, left: Any, right: Any) -> list[Any]:
        from nervos_core.infrastructure.database import create_sqlite_engine

        results: list[Any] = [None, None]
        barrier = threading.Barrier(2)

        def run(index: int, action: Any) -> None:
            local = create_sqlite_engine(self._path)
            try:
                barrier.wait(timeout=10)
                results[index] = action(local)
            except BaseException as error:
                results[index] = error
            finally:
                local.dispose()

        threads = [
            threading.Thread(target=run, args=(0, left)),
            threading.Thread(target=run, args=(1, right)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        return results


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)  # type: ignore[arg-type]


__all__ = [
    "AGENT",
    "DEFAULT_BODY",
    "DEFINITION_ID",
    "NOW",
    "OTHER_OWNER",
    "OWNER",
    "Racer",
    "StaleDefinitionResolver",
    "WebhookRig",
    "build_rig",
    "build_service",
    "migrate",
    "resolved",
]
