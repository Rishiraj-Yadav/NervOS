# LangGraph compatibility matrix (ADR 0039, W4)

Status: one framework version qualified. This is a **narrow, published** matrix, not a
claim of universal SDK compatibility.

## Qualified versions

| Component | Qualified | Declared by the framework |
|---|---|---|
| `langgraph` | **1.2.12** (exact pin) | `>=1.10`, latest release at qualification |
| `langchain-core` | whatever `langgraph==1.2.12` resolves | `>=1.4.7,<2` |
| Python | 3.12+ (workspace) | `>=3.10` |

The pin is exact on purpose. An unqualified LangGraph would make every row below
unverifiable, and a framework's state semantics can change between minors.

The pin lives in `packages/nervos-langgraph/pyproject.toml` and is re-asserted at runtime by
`nervos_langgraph.qualified.assert_qualified_langgraph()`. The adapter **refuses** a version
it was not qualified against instead of adapting it, because an untested release would fail
as a lost or duplicated workflow step rather than as a version mismatch.

## Supported features

| Capability | Supported | Notes |
|---|---|---|
| Plain JSON state (`dict[str, JSON]`) | yes | The only checkpoint shape accepted. |
| Single application node | yes | Exactly START → node → END per Run. |
| Conditional edges, fan-out, cycles, multiple nodes | no | Refused before executing a node. |
| `recursion_limit` bound | yes | Defensive bound; does not authorize multiple nodes. |
| Step state round trip | yes | Next step resumes from the previous checkpoint. |
| `interrupt()` / `Command(resume=...)` | **no** | See "Refusals" below. |
| LangGraph checkpointer / `checkpointer=` | **no** | NervOS is the only durable store. |
| `langgraph.prebuilt` agent executors | no | Not qualified. |
| LangSmith / LangServe tracing | no | Not qualified. |
| Time-travel (`update_state`, checkpoint history) | no | Not qualified. |
| Framework-native serialized state loading | **no** | Never, at any version. |
| LangChain model clients (`ChatOpenAI`, …) in a workflow | no | See "Ports" below. |

## Refusals, and why each is a refusal rather than a gap

### `interrupt()` is refused

Verified against LangGraph 1.2.12: compiled **without** a checkpointer, `interrupt()` does
*not* raise. It returns a state carrying `__interrupt__` and **silently discards the
interrupting node's update**, because there is nothing to resume from. A package that
trusted that return value would see its step recorded as succeeded with the work thrown
away.

So the adapter detects `__interrupt__` and raises `FrameworkDurableWait`. A durable wait
belongs to NervOS: return `WorkflowDirective(kind="wait", …)` and the Scheduler owns the
wakeup, the fence, and the budget.

### Checkpointers are refused

Two stores for one workflow would mean two sources of truth, one of them unfenced. The
NervOS checkpoint is already revision-committed in the same transaction as the Run's
success (ADR 0039 §4), so a LangGraph checkpointer could only be weaker.

### Framework-native serialized state is never loaded

NervOS checkpoints are bounded canonical JSON with a digest and a state version. Loading an
opaque framework blob would mean executing whatever an older release wrote, with no schema,
no bound, and no owner-readable projection. This is refused at every version, not just
unqualified ones.

### Model and tool clients

Models and tools are reached through NervOS ports (`AgentContext.model`, `.tools`), which
means the permission engine, invocation audit, and credential broker all apply. A LangChain
HTTP client (`ChatOpenAI` and friends) would bypass every one of those, so a qualified
workflow may not use one. Bring your own node functions that call the ports instead.

## Deliberately not claimed

* Not universal SDK compatibility. Only the rows above are qualified.
* Not framework state portability. A workflow checkpoint is NervOS JSON, not LangGraph's.
* Not qualified on Windows or macOS. Package execution is refused there (ADR 0035); the
  demos require a supported Linux deployment.

## Evidence

`packages/nervos-langgraph/tests/test_adapter_qualification.py` runs against a real compiled
1.2.12 graph. Each test names the row it justifies: version pinning, state round trip,
restored-state resumption, `interrupt()` refusal, node-budget exhaustion, non-JSON refusal,
and refusal to run on a non-workflow Run.
