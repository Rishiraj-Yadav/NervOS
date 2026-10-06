"""Run one LangGraph graph as exactly one NervOS durable workflow step (ADR 0039).

The rule this module exists to enforce: **NervOS owns durable state; LangGraph owns
control flow inside one step.** That is the opposite of LangGraph's own model, where a
checkpointer is the durable store. Compiling without a checkpointer is not a limitation
here, it is the point:

* The NervOS checkpoint is already fenced, bounded, versioned and revision-committed in the
  same transaction as the Run's success. A second store would be a second, weaker,
  unfenced source of truth for the same workflow.
* LangGraph's own serialization is framework-native and opaque. This adapter will not load
  arbitrary framework-native serialized state, because doing so would mean executing
  whatever an older release happened to write.

One Run executes exactly one application node in a START → node → END graph.
Conditional edges, cycles, multi-node graphs, native stores and checkpointers are
refused before execution. Return a NervOS directive to schedule another durable step.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from nervos_sdk import (
    CHECKPOINT_MAX_BYTES,
    AgentContext,
    AgentResult,
    WorkflowDirective,
    WorkflowResult,
)

from nervos_langgraph.qualified import assert_qualified_langgraph

#: Node budget for one step. LangGraph's own ``recursion_limit`` is passed through as this,
#: so a cyclic graph fails as a clean step error rather than running until the sandbox kills it.
DEFAULT_NODE_LIMIT = 32

#: LangGraph's reserved output key for a pending ``interrupt()``.
_INTERRUPT_KEY = "__interrupt__"


class LangGraphAdapterError(RuntimeError):
    """The graph could not be run as one bounded NervOS workflow step."""


class FrameworkDurableWait(LangGraphAdapterError):
    """The graph asked LangGraph to own a durable wait, which NervOS will not delegate.

    Verified against LangGraph 1.2.12: compiled *without* a checkpointer, ``interrupt()``
    does not raise. It returns a state carrying ``__interrupt__`` and **silently discards the
    node's update**, because there is nowhere to resume from. A package that trusted that
    would see a step "succeed" having lost its work.

    So this is refused explicitly. A durable wait belongs to NervOS: return
    ``WorkflowDirective(kind="wait", ...)`` and let the Scheduler wake the workflow later.
    """


class StepBudgetExceeded(LangGraphAdapterError):
    """The graph ran more nodes in one step than the step's node budget allows."""


class NonSerializableState(LangGraphAdapterError):
    """The graph returned state that is not bounded JSON, so it cannot become a checkpoint."""


@dataclass(frozen=True, slots=True)
class LangGraphStepRunner:
    """Invoke a compiled LangGraph as one NervOS step.

    Deliberately takes an already-compiled graph rather than building one, so the caller
    controls compilation and this class can refuse a graph that was compiled *with* a
    checkpointer -- which would silently reintroduce the second store.
    """

    graph: Any
    node_limit: int = DEFAULT_NODE_LIMIT

    async def run(self, context: AgentContext) -> AgentResult:
        assert_qualified_langgraph()
        graph = getattr(self.graph, "bound", self.graph)
        if getattr(graph, "checkpointer", None) not in (None, False):
            raise FrameworkDurableWait(
                "Use NervOS WorkflowDirective; native checkpointers are forbidden"
            )
        if getattr(graph, "store", None) is not None:
            raise FrameworkDurableWait("Use NervOS state and memory; native stores are forbidden")
        topology = graph.get_graph()
        nodes = set(topology.nodes) - {"__start__", "__end__"}
        edges = {(edge.source, edge.target) for edge in topology.edges}
        if len(nodes) != 1:
            raise StepBudgetExceeded("one ordinary Run may execute exactly one LangGraph node")
        node = next(iter(nodes))
        if edges != {("__start__", node), (node, "__end__")}:
            raise StepBudgetExceeded("cycles, branches and subgraphs are not qualified as one node")
        snapshot = context.workflow
        if snapshot is None:
            raise LangGraphAdapterError(
                "a LangGraph entrypoint may only run as a durable workflow step"
            )
        recursion_error = _recursion_error()
        try:
            output = await self.graph.ainvoke(
                dict(snapshot.state),
                {"recursion_limit": self.node_limit},
            )
        except recursion_error as error:
            raise StepBudgetExceeded(
                f"graph exceeded its {self.node_limit}-node step budget"
            ) from error
        if not isinstance(output, Mapping):
            raise NonSerializableState("graph output must be a state mapping")
        state = dict(cast("Mapping[str, object]", output))
        if _INTERRUPT_KEY in state:
            # Strip LangGraph's private bookkeeping either way: it is framework-native and
            # has no meaning in a NervOS checkpoint.
            state.pop(_INTERRUPT_KEY)
            raise FrameworkDurableWait(
                "interrupt() requires a LangGraph checkpointer, which this adapter does not "
                "use; return WorkflowDirective(kind='wait', ...) so NervOS owns the wakeup"
            )
        final_message = state.pop("final_message", "")
        return AgentResult(
            final_message=final_message if isinstance(final_message, str) else "",
            workflow=WorkflowResult(
                directive=_directive_for(context),
                state=_bounded_state(state),
                summary=_summary(state),
            ),
        )


def _directive_for(context: AgentContext) -> WorkflowDirective:
    """How the step advances, read from the host-supplied configuration.

    The adapter never *chooses* to advance: a graph that runs to completion has either
    finished or has more work, and only the package's own configuration can say which.
    Defaulting to ``next`` keeps a graph that never finished from being silently recorded
    as a completed workflow.
    """
    declared = context.configuration.get("workflow", {})
    directive = declared.get("directive") if isinstance(declared, Mapping) else None
    if directive == "complete":
        return WorkflowDirective(kind="complete")
    return WorkflowDirective(kind="next")


def _summary(state: Mapping[str, Any]) -> str | None:
    value = state.get("summary")
    if isinstance(value, str) and value.strip():
        return value
    return None


def _bounded_state(state: Mapping[str, Any]) -> Mapping[str, Any]:
    """Reject anything that is not bounded JSON *before* the host frames it.

    A checkpoint must be able to survive a restart and be readable by the owner projection.
    Silently coercing an arbitrary Python object would put an unreproducible value into a
    column that claims to be canonical JSON.
    """
    try:
        encoded = json.dumps(
            state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=_json_mapping,
        )
    except (TypeError, ValueError) as error:
        raise NonSerializableState("graph state is not JSON data") from error
    if len(encoded.encode("utf-8")) > CHECKPOINT_MAX_BYTES:
        raise NonSerializableState("graph state exceeds the checkpoint bound")
    return json.loads(encoded)


def _recursion_error() -> type[Exception]:
    """The framework's own recursion error, resolved late.

    Imported inside the function so importing this module does not import LangGraph: a host
    can publish the adapter's compatibility surface without the framework present, and
    ``assert_qualified_langgraph`` still gets to refuse an absent one with a clear message
    before any graph is touched.
    """
    try:
        from langgraph.errors import GraphRecursionError
    except ImportError:  # pragma: no cover - guarded by the version check above it
        return RuntimeError
    return GraphRecursionError


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(cast(Mapping[str, Any], value))
    raise TypeError("state is not JSON data")


__all__ = [
    "DEFAULT_NODE_LIMIT",
    "FrameworkDurableWait",
    "LangGraphAdapterError",
    "LangGraphStepRunner",
    "NonSerializableState",
    "StepBudgetExceeded",
]
