"""Stage E3 query plans: the two reads the ingress performs are index seeks, not scans.

E3 adds no index, because `0008` already carries exactly the two access paths a delivery needs.
These tests prove the *real* methods use them, rather than asserting a hand-written query that
merely resembles what the code does: the statements are captured from the engine while the
composed ingress runs, and each captured statement is then planned against a populated database.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, event, text
from webhook_support import DEFAULT_BODY, NOW, OWNER, WebhookRig, build_rig


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[WebhookRig]:
    built = build_rig(tmp_path / "plans.db", monkeypatch)
    try:
        yield built
    finally:
        built.engine.dispose()


class Captured:
    """Every statement an engine executed between `enter` and `exit`, with its parameters."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self.statements: list[tuple[str, Any]] = []

    def __enter__(self) -> Captured:
        def record(
            conn: Any,
            cursor: Any,
            statement: str,
            parameters: Any,
            context: Any,
            executemany: bool,
        ) -> None:
            del conn, cursor, context, executemany
            self.statements.append((statement, parameters))

        self._handler = record
        event.listen(self._engine, "before_cursor_execute", record)
        return self

    def __exit__(self, *_: object) -> None:
        event.remove(self._engine, "before_cursor_execute", self._handler)

    def selects(self, *needles: str) -> list[tuple[str, Any]]:
        return [
            (statement, parameters)
            for statement, parameters in self.statements
            if statement.lstrip().upper().startswith("SELECT")
            and all(needle in statement for needle in needles)
        ]

    def one(self, *needles: str) -> tuple[str, Any]:
        found = self.selects(*needles)
        assert len(found) == 1, f"expected one SELECT for {needles}: {[s for s, _ in found]}"
        return found[0]


def plan_for(engine: Engine, captured: tuple[str, Any]) -> str:
    """Plan the statement the engine actually ran, with its own bindings supplied."""
    statement, parameters = captured
    with engine.connect() as connection:
        rows = connection.exec_driver_sql(f"EXPLAIN QUERY PLAN {statement}", parameters).all()
    return " | ".join(str(row[-1]) for row in rows)


def populate(rig: WebhookRig, *, triggers: int = 40, deliveries: int = 40) -> None:
    """Enough rows that a scan would be plainly visible in the plan."""
    for index in range(triggers):
        issued = rig.provision(display_name=f"Hook {index}", input_text=f"Handle {index}.")
        if index == 0:
            for delivery in range(deliveries):
                rig.deliver(issued, idempotency_key=f"key-{delivery}")


def test_the_locator_lookup_is_one_index_seek(rig: WebhookRig) -> None:
    populate(rig)
    issued = rig.provision()

    with Captured(rig.engine) as captured:
        found = rig.triggers.find_webhook_by_public_id(issued.trigger.public_id or "")

    assert found is not None
    plan = plan_for(rig.engine, captured.one("trigger_definitions.public_id = ?"))
    assert "ux_trigger_definitions_public_id" in plan, plan
    assert "SCAN trigger_definitions" not in plan, plan


def test_the_idempotency_lookup_is_one_index_seek(rig: WebhookRig) -> None:
    populate(rig)
    issued = rig.provision()
    rig.deliver(issued, idempotency_key="key-0")

    with Captured(rig.engine) as captured:
        result = rig.deliver(issued, idempotency_key="key-0")

    assert result.duplicate is True
    plan = plan_for(rig.engine, captured.one("trigger_occurrences.idempotency_key = ?"))
    assert "ux_trigger_occurrences_idempotency_identity" in plan, plan
    assert "SCAN trigger_occurrences" not in plan, plan


def test_a_keyless_delivery_performs_no_identity_lookup(rig: WebhookRig) -> None:
    """With no deterministic identity there is nothing to look up, so no query is issued."""
    issued = rig.provision()

    with Captured(rig.engine) as captured:
        result = rig.deliver(issued, body=DEFAULT_BODY)

    assert result.kind.value == "materialized"
    assert captured.selects("trigger_occurrences.idempotency_key = ?") == []


def test_the_delivery_writes_only_the_rows_it_must(rig: WebhookRig) -> None:
    """One occurrence insert and the canonical Run/Job/event writes, and nothing else."""
    issued = rig.provision()
    rig.deliver(issued)

    before = rig.counts()
    assert before == {
        "runs": 1,
        "jobs": 1,
        "job_attempts": 0,
        "run_events": 2,
        "trigger_occurrences": 1,
    }

    rig.deliver(issued, idempotency_key="another")
    after = rig.counts()
    assert after["trigger_occurrences"] == before["trigger_occurrences"] + 1
    assert after["runs"] == before["runs"] + 1


def test_the_trigger_row_is_never_updated_by_a_delivery(rig: WebhookRig) -> None:
    """A delivery-driven trigger has no schedule state to advance."""
    issued = rig.provision()
    with rig.engine.connect() as connection:
        before = connection.execute(
            text(
                "SELECT config_revision, next_fire_at, enabled FROM trigger_definitions "
                "WHERE id = :id"
            ),
            {"id": issued.trigger.id},
        ).one()

    rig.deliver(issued)

    with rig.engine.connect() as connection:
        after = connection.execute(
            text(
                "SELECT config_revision, next_fire_at, enabled FROM trigger_definitions "
                "WHERE id = :id"
            ),
            {"id": issued.trigger.id},
        ).one()
    assert tuple(after) == tuple(before)
    assert OWNER == 1 and NOW.year == 2026
