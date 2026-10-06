"""Qualification for the pinned LangGraph adapter (W4b).

These tests are the evidence behind the published compatibility matrix. They are written
against a *real* compiled LangGraph 1.2.12 graph, not a fake, because the properties that
matter here are properties of the framework: what ``interrupt()`` does without a
checkpointer, what ``recursion_limit`` raises, and what comes back from ``ainvoke``.

Each test names the matrix row it justifies. If a row is ever widened, the test that
justifies it has to change first.
"""

# pyright: basic
# LangGraph 1.2.12's own generics are not strict-clean, so strict mode here would report
# errors in the framework's signatures rather than in anything this repository asserts. The
# production adapter stays strict; only these qualification tests relax, and they still
# assert the behaviour those types describe.

from __future__ import annotations

import re
import typing
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError
from typing import Any

import pytest
from nervos_langgraph import (
    QUALIFIED_LANGGRAPH_VERSION,
    FrameworkDurableWait,
    LangGraphAdapterError,
    LangGraphStepRunner,
    NonSerializableState,
    StepBudgetExceeded,
    UnqualifiedLangGraph,
    assert_qualified_langgraph,
    installed_langgraph_version,
)
from nervos_sdk import AgentContext, JSONValue, WorkflowSnapshot

pytest.importorskip("langgraph", reason="the pinned LangGraph adapter is not installed")

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from nervos_langgraph import qualified


class State(typing.TypedDict, total=False):
    """A deliberately plain state: JSON scalars only, like every NervOS checkpoint."""

    count: int
    sources: list[str]
    summary: str
    final_message: str


def _sources(state: Mapping[str, JSONValue]) -> list[str]:
    """The checkpoint's ``sources`` entry, narrowed from the SDK's frozen JSON union.

    The SDK freezes sequences to tuples on the way in, so a round-tripped list arrives here
    as a tuple. Accepting either is the honest shape; asserting ``list`` would be asserting
    an accident of the caller's dictionary literal rather than the contract.
    """
    value = state["sources"]
    assert isinstance(value, list | tuple)
    return [str(item) for item in value]


def _context(state: Mapping[str, JSONValue] | None = None) -> AgentContext:
    return AgentContext(
        run_id="run-1",
        agent_instance_id="agent-1",
        workflow=WorkflowSnapshot(
            step_number=2,
            checkpoint_revision=3,
            state=state or {"count": 1, "sources": ["a"]},
        ),
    )


def _compile(nodes: dict[str, Any], edges: list[tuple[str, str]]) -> Any:
    """Compile a graph with NO checkpointer: NervOS is the only durable store.

    Typed as ``Any`` on purpose. LangGraph's node/edge generics are overload sets over
    dataclass-like state schemas, so under any checking mode the errors reported here would
    describe *LangGraph's* signatures rather than anything this repository claims. The
    behaviour under test is asserted below; the construction is incidental.
    """
    builder: Any = StateGraph(State)
    for name, action in nodes.items():
        builder.add_node(name, action)
    for source, target in edges:
        builder.add_edge(source, target)
    return builder.compile()


def _linear_graph() -> Any:
    return _compile(
        {"collect": lambda s: {"sources": [*s.get("sources", []), "b"]}},
        [(START, "collect"), ("collect", END)],
    )


# ---------------------------------------------------------------------------------------
# Matrix row: pinned version
# ---------------------------------------------------------------------------------------


def test_the_installed_langgraph_is_exactly_the_qualified_one() -> None:
    assert installed_langgraph_version() == QUALIFIED_LANGGRAPH_VERSION
    assert assert_qualified_langgraph() == QUALIFIED_LANGGRAPH_VERSION


def test_an_unqualified_langgraph_is_refused_rather_than_adapted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Adapting an untested release would surface as a lost or duplicated workflow step, not
    # as a version error, so the mismatch has to be caught at the boundary.
    monkeypatch.setattr(qualified, "version", lambda _name: "1.3.0")
    with pytest.raises(UnqualifiedLangGraph, match=re.escape("1.3.0")):
        assert_qualified_langgraph()


def test_a_missing_langgraph_is_refused_rather_than_silently_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _absent(_name: str) -> str:
        raise PackageNotFoundError("langgraph")

    monkeypatch.setattr(qualified, "version", _absent)
    with pytest.raises(UnqualifiedLangGraph, match="not installed"):
        assert_qualified_langgraph()


# ---------------------------------------------------------------------------------------
# Matrix row: state round trip
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_graph_round_trips_state_into_a_nervos_checkpoint() -> None:
    result = await LangGraphStepRunner(_linear_graph()).run(_context())
    assert result.workflow is not None
    assert _sources(result.workflow.state) == ["a", "b"]
    assert result.workflow.state["count"] == 1


@pytest.mark.anyio
async def test_the_next_checkpoint_is_built_from_the_restored_state() -> None:
    # Step 2 resumes from step 1's output. If the adapter passed only the *input*, the
    # graph would restart from scratch and silently lose a whole step of work.
    result = await LangGraphStepRunner(_linear_graph()).run(
        _context({"count": 5, "sources": ["a", "b"]})
    )
    assert result.workflow is not None
    assert _sources(result.workflow.state) == ["a", "b", "b"]
    assert result.workflow.state["count"] == 5


@pytest.mark.anyio
async def test_a_step_defaults_to_advancing_rather_than_completing() -> None:
    result = await LangGraphStepRunner(_linear_graph()).run(_context())
    assert result.workflow is not None
    assert result.workflow.directive.kind == "next"


@pytest.mark.anyio
async def test_a_package_can_declare_that_its_graph_finished() -> None:
    context = AgentContext(
        run_id="run-1",
        agent_instance_id="agent-1",
        configuration={"workflow": {"directive": "complete"}},
        workflow=WorkflowSnapshot(step_number=2, checkpoint_revision=3, state={"count": 1}),
    )
    result = await LangGraphStepRunner(_linear_graph()).run(context)
    assert result.workflow is not None
    assert result.workflow.directive.kind == "complete"


# ---------------------------------------------------------------------------------------
# Matrix row: durable waits are NervOS's, not the framework's
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_interrupt_is_refused_instead_of_silently_losing_the_step() -> None:
    # Without a checkpointer, LangGraph 1.2.12 does NOT raise on interrupt(): it returns
    # `__interrupt__` and discards the node's update. Trusting that would record a step as
    # succeeded with the work thrown away, so the adapter refuses it explicitly.
    def ask(state: State) -> State:
        approved = interrupt({"question": "approve?"})
        return {"count": state.get("count", 0) + int(approved)}

    graph = _compile({"ask": ask}, [(START, "ask"), ("ask", END)])
    with pytest.raises(FrameworkDurableWait, match="WorkflowDirective"):
        await LangGraphStepRunner(graph).run(_context())


# ---------------------------------------------------------------------------------------
# Matrix row: boundedness
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_cyclic_graph_fails_the_step_rather_than_running_forever() -> None:
    graph = _compile(
        {
            "a": lambda s: {"count": s.get("count", 0) + 1},
            "b": lambda s: {"count": s.get("count", 0) + 1},
        },
        [(START, "a"), ("a", "b"), ("b", "a")],
    )
    with pytest.raises(StepBudgetExceeded):
        await LangGraphStepRunner(graph, node_limit=6).run(_context())


@pytest.mark.anyio
async def test_state_that_is_not_json_is_refused() -> None:
    graph = _compile({"bad": lambda s: {"summary": object()}}, [(START, "bad"), ("bad", END)])
    with pytest.raises(NonSerializableState):
        await LangGraphStepRunner(graph).run(_context())


@pytest.mark.anyio
async def test_native_checkpointer_is_refused_before_any_node_runs() -> None:
    from langgraph.checkpoint.memory import InMemorySaver

    calls = []
    builder: Any = StateGraph(State)
    builder.add_node("a", lambda s: calls.append("a") or {"count": 2})
    builder.add_edge(START, "a")
    builder.add_edge("a", END)
    graph = builder.compile(checkpointer=InMemorySaver()).with_config(
        {"configurable": {"thread_id": "fixture"}}
    )
    with pytest.raises(FrameworkDurableWait, match="checkpointer"):
        await LangGraphStepRunner(graph).run(_context())
    assert calls == []


@pytest.mark.anyio
async def test_multi_node_graph_is_refused_before_execution() -> None:
    calls = []
    graph = _compile(
        {"a": lambda s: calls.append("a") or s, "b": lambda s: calls.append("b") or s},
        [(START, "a"), ("a", "b"), ("b", END)],
    )
    with pytest.raises(StepBudgetExceeded, match="one"):
        await LangGraphStepRunner(graph).run(_context())
    assert calls == []


@pytest.mark.anyio
async def test_an_ordinary_run_may_not_use_the_langgraph_adapter() -> None:
    # A graph on a non-workflow Run has nowhere to put its state, so it must not appear to
    # succeed and then be dropped.
    ordinary = AgentContext(run_id="run-1", agent_instance_id="agent-1")
    with pytest.raises(LangGraphAdapterError, match="durable workflow step"):
        await LangGraphStepRunner(_linear_graph()).run(ordinary)
