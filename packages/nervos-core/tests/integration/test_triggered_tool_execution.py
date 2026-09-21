"""Stage E5 focused traceability: a Trigger-created Run is an ordinary Stage-D tool-enabled Run.

E5's accepted contract requires that a Run born from a Stage-E trigger traverse the *ordinary*
Stage-C execution composition and the *ordinary* Stage-D tool loop -- one ToolInvocation, a
concluding model turn, and a terminal Run. The E2/E3/E4 suites prove the materializer creates an
ordinary Run/Job through the canonical `insert_run_and_job_on_connection` seam; the D7 suites prove
an ordinary submitted Run executes tools. This single test closes the traceability gap: it creates a
Run through the real scheduler trigger-materialization seam and then executes that exact Run through
the real Worker composition, proving the two halves share the one execution path.

No production code, no real provider, and no browser are involved. The model is a scripted
completion over a frozen clock, so provider accounting stays deterministic (the scheduler never
invokes a tool itself -- the Worker's loop does, exactly as for a manually submitted Run).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from d6_support import Rig
from nervos_core.application.agent_definitions import create_builtin_definition_registry
from nervos_core.application.builtin_tools import (
    builtin_tool_specs,
    reconcile_builtin_definitions,
)
from nervos_core.application.scheduler import SchedulerService
from nervos_core.application.tool_invocations import InvocationStatus
from nervos_core.application.triggers import TriggerDraft
from nervos_core.domain.triggers import ScheduleSpec, TriggerKind
from nervos_core.infrastructure.database.tool_definitions import (
    SqlAlchemyToolDefinitionPersistence,
)
from nervos_core.infrastructure.database.triggers import SqlAlchemyTriggerPersistence
from nervos_core.infrastructure.scheduling import create_schedule_evaluator
from scheduler_support import AGENT, NOW, OWNER, migrate
from sqlalchemy import text
from stage_d_support import (
    ScriptedCompletion,
    build_execution,
    execute_run,
    final_turn,
    grant,
    invitations,
    tool_turn,
)


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    """A migrated DB with the tool-enabled definition and one granted tool, ready for a trigger.

    `scheduler_support.migrate` seeds the Agent Instance as `nervos.chat@2`, the tool-enabled
    definition, so a Run the trigger materializer creates resolves to tool-enabled limits -- the
    same limits a Run the Stage-D harness submits carries.
    """
    engine = migrate(tmp_path / "nervos.db", monkeypatch)
    definitions = SqlAlchemyToolDefinitionPersistence(engine)
    descriptors = reconcile_builtin_definitions(
        definitions, specs=builtin_tool_specs(clock=lambda: NOW), now=NOW
    )
    d_rig = Rig(engine=engine, descriptors=tuple(descriptors), instance_id=AGENT)
    # Reconcile registers; it does not grant. Grant exactly as a user's review would.
    current_time = next(d for d in descriptors if d.upstream_name == "current_time")
    grant(d_rig, descriptor=current_time)
    return d_rig


def test_a_trigger_created_run_reaches_the_tool_loop_and_concludes(rig: Rig) -> None:
    engine = rig.engine
    triggers = SqlAlchemyTriggerPersistence(engine, sleep=lambda _: None)
    scheduler = SchedulerService(
        create_builtin_definition_registry(), triggers, create_schedule_evaluator()
    )

    # Create one interval trigger already due at NOW through the real creation port.
    draft = TriggerDraft(
        agent_instance_id=AGENT,
        display_name="Tool triggered",
        input_text="what time is it",
        kind=TriggerKind.INTERVAL,
        schedule=ScheduleSpec.interval(300),
        enabled=True,
        next_fire_at=NOW,
    )
    trigger = triggers.create_trigger(OWNER, draft, NOW)

    # Materialize the due schedule: this is where the trigger creates the Run/Job.
    scheduler.tick(NOW)

    with engine.connect() as connection:
        run_id = int(
            connection.execute(text("SELECT id FROM runs ORDER BY id LIMIT 1")).scalar_one()
        )
        occurrence = connection.execute(
            text(
                "SELECT status, run_id FROM trigger_occurrences "
                "WHERE trigger_definition_id = :trigger"
            ),
            {"trigger": trigger.id},
        ).one()

    current_time = next(d for d in rig.descriptors if d.upstream_name == "current_time")
    completion = ScriptedCompletion(
        tool_turn(current_time.model_name, '{"timezone":"UTC"}', call_id="call-1"),
        final_turn("the time is known"),
    )
    stage = build_execution(rig, completion=completion)
    outcome = asyncio.run(execute_run(stage, run_id))

    # The Trigger-created Run ended as an ordinary, terminal, successful Run.
    assert outcome is not None and outcome.status == "succeeded"
    assert outcome.output_text == "the time is known"
    # The model was consulted exactly twice: once to request the tool, once to conclude.
    assert len(completion.requests) == 2

    with engine.connect() as connection:
        status = str(
            connection.execute(
                text("SELECT status FROM runs WHERE id = :id"), {"id": run_id}
            ).scalar_one()
        )
    assert status == "succeeded"

    # One ToolInvocation was recorded, and it succeeded.
    rows = invitations(rig)
    assert [row["status"] for row in rows] == [InvocationStatus.SUCCEEDED.value]

    # The occurrence is the RUN_CREATED one, pointing at the Run that just executed.
    assert occurrence[0] == "run_created"
    assert occurrence[1] == run_id
