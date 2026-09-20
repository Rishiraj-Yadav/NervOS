"""Stage E3 webhook races, with no sleeps anywhere.

Two kinds of test live here, and the difference matters.

The **deterministic orderings** pin ADR 0019's frozen table (0019:116-120) directly: rotate, then
deliver; deliver, then rotate. There is no race to resolve because the order is imposed, so the
assertion can be exact.

The **barrier races** release two real transactions at the same instant, each on its own connection
to the same file, and SQLite's write lock decides the winner. Which side wins is not deterministic
and is not asserted; what is asserted is the **invariant that must hold either way** -- a delivery
that never authenticated never wrote anything, and one that authenticated wrote exactly one
occurrence.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from nervos_core.application.webhooks import (
    WebhookDelivery,
    WebhookDeliveryKind,
    WebhookDeliveryService,
)
from nervos_core.infrastructure.database.triggers import SqlAlchemyTriggerPersistence
from nervos_core.infrastructure.security.webhook_secrets import digest_secret, generate_secret
from sqlalchemy import text
from webhook_support import DEFAULT_BODY, NOW, OWNER, Racer, WebhookRig, build_rig
from webhook_support import build_service as compose_service


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[WebhookRig]:
    """A synchronous rig. The races below are thread-based, not async."""
    built = build_rig(tmp_path / "races.db", monkeypatch)
    try:
        yield built
    finally:
        built.engine.dispose()


def _persistence(engine: Any) -> SqlAlchemyTriggerPersistence:
    return SqlAlchemyTriggerPersistence(engine, sleep=lambda _: None)


def _service(engine: Any) -> WebhookDeliveryService:
    return compose_service(_persistence(engine))


def _deliver_action(
    *, public_id: str, secret: str | None, idempotency_key: str | None = None
) -> Any:
    def action(engine: Any) -> Any:
        return _service(engine).receive(
            WebhookDelivery(
                public_id=public_id,
                candidate_secret=secret,
                idempotency_key=idempotency_key,
                has_conflicting_idempotency_keys=False,
                is_json_content_type=True,
                occurred_at=NOW,
            ),
            DEFAULT_BODY,
        )

    return action


# ------------------------------------------------------------------------------------------------
# Deterministic orderings: ADR 0019's frozen table
# ------------------------------------------------------------------------------------------------


def test_a_rotation_that_commits_first_refuses_the_old_secret(rig: WebhookRig) -> None:
    issued = rig.provision()
    rig.rotate(issued.trigger.id)

    result = rig.deliver(issued, secret=issued.secret)

    assert result.kind is WebhookDeliveryKind.UNAUTHENTICATED
    assert rig.counts()["trigger_occurrences"] == 0
    assert rig.counts()["runs"] == 0


def test_a_delivery_that_commits_first_survives_a_later_rotation(rig: WebhookRig) -> None:
    issued = rig.provision()
    delivered = rig.deliver(issued, secret=issued.secret, idempotency_key="key-1")

    rig.rotate(issued.trigger.id)

    assert delivered.kind is WebhookDeliveryKind.MATERIALIZED
    assert rig.counts()["runs"] == 1
    assert rig.counts()["trigger_occurrences"] == 1


def test_a_disable_that_commits_first_refuses_a_fresh_delivery(rig: WebhookRig) -> None:
    issued = rig.provision()
    rig.set_enabled(issued.trigger.id, False)

    result = rig.deliver(issued)

    assert result.kind is WebhookDeliveryKind.DISABLED
    assert rig.counts()["runs"] == 0


def test_a_delivery_that_commits_first_survives_a_later_disable(rig: WebhookRig) -> None:
    issued = rig.provision()
    delivered = rig.deliver(issued)

    rig.set_enabled(issued.trigger.id, False)

    assert delivered.kind is WebhookDeliveryKind.MATERIALIZED
    assert rig.counts()["runs"] == 1


# ------------------------------------------------------------------------------------------------
# Barrier races: correct whichever side wins
# ------------------------------------------------------------------------------------------------


def test_two_concurrent_deliveries_of_one_key_produce_exactly_one_run(rig: WebhookRig) -> None:
    """The unique index is the arbiter, and the loser is answered with the winner's row."""
    issued = rig.provision()
    public_id = issued.trigger.public_id or ""
    action = _deliver_action(public_id=public_id, secret=issued.secret, idempotency_key="key-1")

    results = Racer(rig.path).race(action, action)

    for result in results:
        assert not isinstance(result, BaseException), result
    kinds = sorted(result.kind.value for result in results)
    assert kinds == ["duplicate", "materialized"]
    assert rig.counts()["trigger_occurrences"] == 1
    assert rig.counts()["runs"] == 1
    assert rig.counts()["jobs"] == 1
    # Both callers are told about the same occurrence.
    assert len({result.occurrence_id for result in results}) == 1


def test_a_delivery_racing_a_rotation_never_writes_for_a_stale_credential(rig: WebhookRig) -> None:
    """Either order is acceptable; what must hold is that no unauthenticated delivery wrote.

    If the rotation committed first the delivery is refused and nothing exists. If the delivery
    committed first it authenticated against the secret that was current at that moment, and its
    occurrence exists. Neither combination may be mixed.
    """
    issued = rig.provision()
    public_id = issued.trigger.public_id or ""
    replacement = generate_secret()

    def rotate(engine: Any) -> Any:
        return _persistence(engine).rotate_webhook_secret(
            OWNER, issued.trigger.id, digest_secret(replacement), NOW
        )

    results = Racer(rig.path).race(
        _deliver_action(public_id=public_id, secret=issued.secret), rotate
    )
    delivered = results[0]
    assert not isinstance(delivered, BaseException), delivered

    occurrences = rig.counts()["trigger_occurrences"]
    runs = rig.counts()["runs"]
    if delivered.kind is WebhookDeliveryKind.MATERIALIZED:
        assert (occurrences, runs) == (1, 1)
    else:
        assert delivered.kind is WebhookDeliveryKind.UNAUTHENTICATED
        assert (occurrences, runs) == (0, 0)


def test_a_delivery_racing_a_disable_never_writes_for_a_disabled_trigger(rig: WebhookRig) -> None:
    issued = rig.provision()
    public_id = issued.trigger.public_id or ""

    def disable(engine: Any) -> Any:
        _persistence(engine).set_enabled(OWNER, issued.trigger.id, False, NOW)
        return "disabled"

    results = Racer(rig.path).race(
        _deliver_action(public_id=public_id, secret=issued.secret), disable
    )
    delivered = results[0]
    assert not isinstance(delivered, BaseException), delivered

    occurrences = rig.counts()["trigger_occurrences"]
    if delivered.kind is WebhookDeliveryKind.MATERIALIZED:
        assert occurrences == 1
    else:
        assert delivered.kind is WebhookDeliveryKind.DISABLED
        assert occurrences == 0


def test_a_delivery_racing_a_retargeted_agent_never_runs_the_wrong_definition(
    rig: WebhookRig,
) -> None:
    """A materialized Run carries the definition the row actually had when it was written."""
    issued = rig.provision()
    public_id = issued.trigger.public_id or ""

    def retarget(engine: Any) -> Any:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE agent_instances SET agent_definition_version = '1' WHERE id = 1")
            )
        return "retargeted"

    results = Racer(rig.path).race(
        _deliver_action(public_id=public_id, secret=issued.secret), retarget
    )
    delivered = results[0]
    assert not isinstance(delivered, BaseException), delivered

    runs = rig.counts()["runs"] if delivered.kind is WebhookDeliveryKind.MATERIALIZED else 0
    if delivered.kind is WebhookDeliveryKind.MATERIALIZED:
        assert delivered.run_id is not None
        run = rig.run_row(delivered.run_id)
        # The snapshot came from the durable row, so it is one of the two real versions -- never a
        # mixture, and never a definition the transaction did not verify.
        assert run["agent_definition_version"] in {"1", "2"}
        assert runs == 1
    else:
        assert delivered.kind is WebhookDeliveryKind.TEMPORARILY_UNAVAILABLE
        assert rig.counts()["trigger_occurrences"] == 0
        assert rig.counts()["runs"] == 0


# ------------------------------------------------------------------------------------------------
# Rotation and duplicate history
# ------------------------------------------------------------------------------------------------


def test_a_rotated_trigger_still_refuses_the_old_secret_on_a_retry(rig: WebhookRig) -> None:
    issued = rig.provision()
    first = rig.deliver(issued, idempotency_key="key-1")
    rotated = rig.rotate(issued.trigger.id)

    stale = rig.deliver(issued, secret=issued.secret, idempotency_key="key-1")
    current = rig.deliver(rotated, secret=rotated.secret, idempotency_key="key-1")

    assert first.kind is WebhookDeliveryKind.MATERIALIZED
    assert stale.kind is WebhookDeliveryKind.UNAUTHENTICATED
    assert stale.occurrence_id is None
    assert current.kind is WebhookDeliveryKind.DUPLICATE
    assert current.occurrence_id == first.occurrence_id
    assert rig.counts()["trigger_occurrences"] == 1
    assert rig.counts()["runs"] == 1


def test_a_rotated_credential_is_the_only_one_that_can_create_a_second_run(rig: WebhookRig) -> None:
    issued = rig.provision()
    rotated = rig.rotate(issued.trigger.id)

    assert rig.deliver(issued, secret=issued.secret).kind is WebhookDeliveryKind.UNAUTHENTICATED
    assert rig.deliver(rotated, secret=rotated.secret).kind is WebhookDeliveryKind.MATERIALIZED
    assert rig.counts()["runs"] == 1
