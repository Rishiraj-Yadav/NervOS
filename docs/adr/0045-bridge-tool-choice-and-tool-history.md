# ADR 0045 — Bridge Tool Choice and Tool-Aware History

**Status: ACCEPTED** — Implemented per the Owner decisions below.

## Context

ADR 0042 refused two shapes the unmodified SDKs send routinely:

1. **Forced tool choice** — `tool_choice: "required"`, a named function (OpenAI Chat and
   Responses), `{"type":"any"}` / `{"type":"tool"}` (Anthropic), `mode: ANY` with
   `allowedFunctionNames` (Gemini). The core `ModelRequest` had no tool-choice field and
   all three adapters hardcoded automatic choice.
2. **Tool-aware history** — a conversation in which a tool exchange precedes a later
   user message (agent memory). The core carried tool traffic only in `turns`, which
   follow the single current `user_text`, and `history` was text-only, so the Worker
   refused such requests with `model_request_rejected` rather than rewriting history.

## Decision

### B2 — Tool choice (additive, core)

`nervos_core.application.model_completion` gains:

```python
class ToolChoiceKind(StrEnum):
    AUTO, NONE, REQUIRED, NAMED


@dataclass(frozen=True, slots=True)
class ToolChoice:
    kind: ToolChoiceKind = AUTO
    name: str | None = None  # required iff kind is NAMED
```

`ModelRequest.tool_choice: ToolChoice` defaults to `ToolChoice()` — a request that
never sets it is unchanged. Adapters map it to each vendor's real parameters and never
emulate:

| kind | OpenAI (Responses) | Anthropic Messages | Gemini |
|---|---|---|---|
| auto (default) | `tool_choice="auto"` (byte-identical to before) | `{"type":"auto","disable_parallel_tool_use":true}` (identical) | `mode=AUTO` (identical) |
| none | `"none"` | `{"type":"none"}` | `mode=NONE` |
| required | `"required"` | `{"type":"any","disable_parallel_tool_use":true}` | `mode=ANY` |
| named | `{"type":"function","name":N}` | `{"type":"tool","name":N,"disable_parallel_tool_use":true}` | `mode=ANY, allowed_function_names=[N]` |

`required`/`named` without offered tools is refused with `model_request_rejected`
(the neutral request cannot express forcing a tool that is not offered). No provider
lacks a real parameter for any mode, so no mode is emulated.

### B3 — Tool-aware history (additive, bridge-only field)

The smallest additive change is a **new optional ordered conversation** on the core
`ModelRequest`:

```python
@dataclass(frozen=True, slots=True)
class UserTurn:
    text: str


type ConversationItem = AssistantTurn | ToolResultTurn | UserTurn

conversation: tuple[ConversationItem, ...] = ()
```

Used **only when non-empty**: adapters then build the provider message list from the
ordered items (`system_instruction` still leads) and ignore `history`, `user_text` and
`turns`. When it is empty, every adapter's layout is byte-identical to before.

**Why it is not persisted:** `ModelRequest` is a per-call object built inside
`complete_model`; nothing writes it to the database or to a durable context snapshot
(`ContextSnapshotData` carries its own `HistoricalMessage` tuple, which this ADR does
not touch, and no new field was added to any persisted type). The Worker only sets
`conversation` for bridge requests whose tool traffic precedes a later user message;
everything else keeps the existing history/user_text/turns mapping.

### B4 — Plumbing

- SDK (`nervos_sdk.types`): `ModelRequest.tool_choice` — a public `ToolChoice`
  (kind auto/none/required/named + optional name), defaulted, frozen. The ordered
  conversation needs no new SDK field: `ModelMessage` already carries full order via
  roles plus `tool_calls`/`tool_call_id`.
- Wire: `model_request` payload gains an optional `tool_choice`
  (`{"kind": ..., "name": ...}`). Frames without it remain valid.
- Worker (`complete_model`): parses the wire tool choice (unknown kinds, or a named
  tool not among the declared tools after filtering, are `model_request_rejected`) and
  builds `conversation` instead of refusing when tool traffic precedes the last user
  message.
- Shim routes (all four): accept each vendor's forced-choice forms, require a named
  choice to be among the offered tools (400 otherwise), forward `tool_choice` on the
  wire. Tool offers remain filtered to the run's declared tools worker-side.

## Consequences

- `tool_choice` reaches the provider verbatim in every vendor's vocabulary; nothing is
  emulated and refusals stay `model_request_rejected`.
- Multi-turn tool conversations now flow through the neutral request in order, without
  rewriting history: the whole conversation is carried as `conversation`.
- Existing behavior is untouched whenever the new fields are absent (default tool
  choice; conversations without tool traffic before the last user message).

## Verification

- Adapter fake-client tests asserting the exact kwargs per mode, including that a
  default request produces the exact pre-ADR kwargs.
- `apps/worker/tests/unit/test_model_tool_mediation.py`: the former refusal test now
  asserts the multi-turn shape succeeds via `conversation`.
- Real-socket suites: forced tool choice per route and a multi-turn tool conversation
  per route, with the wire frames asserted.
