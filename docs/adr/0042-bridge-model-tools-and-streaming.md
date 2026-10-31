# ADR 0042 — Bridge Model Tools and Streaming

**Status: ACCEPTED** — Implemented per the Owner decisions below.

## Context

ADR 0041 delivered the generic process agent bridge: a loopback shim inside the package
host translating OpenAI-compatible `POST /v1/chat/completions` and MCP `/mcp` requests
into the existing `model_request` / `tool_request` wire frames. But the chat endpoint
accepted text-only conversations and rejected `tools` and `stream: true`, so unmodified
agent SDKs (LangChain, CrewAI, Pydantic AI, OpenAI SDK) could not actually drive an
agent loop through NervOS — the model could never ask for a tool.

The core already speaks tools: `nervos_core.application.model_completion.ModelRequest`
carries `tools: tuple[ToolSchema, ...]` and multi-turn `turns` (`AssistantTurn` with
`ToolCall`s, `ToolResultTurn`), `ModelResponse` carries `tool_calls`, and the provider
adapters in `nervos-models` already normalize provider tool calls. Stage D's loop and
audit already mediate tool execution via `ToolInvocationMediator`.

## Decision

The shim's `POST /v1/chat/completions` supports function calling and `stream: true`,
additively. Every new field on the wire and in the SDK types is optional with a default,
so a tool-free request is byte-for-byte what it was before.

### Owner decisions

1. **No real token streaming.** The Worker still returns one full
   `model_response`; the shim awaits it and replays it as valid SSE
   (`data: {...}` chunks, tool_call deltas with indexes and string arguments, a final
   chunk with `finish_reason`, an optional usage chunk for
   `stream_options.include_usage`, then `data: [DONE]`).
2. **No images, audio, embeddings, or provider-hosted tools.** Non-text content parts
   and non-`function` tool types get a clear OpenAI-style 400.
3. **No new shipped third-party dependencies.** Shim and host use the standard library
   only, as before.
4. **No live Linux/sandbox testing and no real provider calls.** Verification is the
   real-socket suite (real shim, fake wire Worker, real stdlib-only child process);
   Linux loopback stays `[unverified]` as in ADR 0041.
5. **Anthropic, Responses, and Gemini endpoints are NOT built** (deferred to Part 2).
6. **`tool_choice`** is honored where the neutral request can express it: `auto`
   (default) passes the tools through, `none` drops them before the wire;
   `required` and named selection are refused with 400 because the core
   `ModelRequest` has no forced-tool field and this ADR does not change the core.

### What was added, layer by layer

- **SDK (`nervos_sdk.types`)**: optional `tools` on `ModelRequest`
  (`ToolDefinition`: name, description, JSON-schema parameters); optional `tool_calls`
  (id, name, JSON-text-or-mapping arguments) and `tool_call_id` on `ModelMessage`;
  optional `tool_calls` on `ModelResult` and the `STOP_REASON_TOOL_USE = "tool_use"`
  convention. All frozen dataclasses with JSON freezing, all defaulted.
- **Wire**: `model_request` frames may carry `tools` and per-message
  `tool_calls` / `tool_call_id`; `model_response` frames may carry `tool_calls`.
  Advertised additively through the existing capability list as
  `model-tools-v1` in `HOST_CAPABILITIES`. Frames without the new fields remain valid.
- **Worker (`package_execution.py`)**: `complete_model` maps the wire conversation onto
  the core request — leading systems to `system_instruction`, plain history before the
  current user turn to `history`, the current user message to `user_text`, and the
  trailing assistant/tool exchange to `turns`. Offered tools are validated, capped
  (≤64 tools; text + tool traffic + schemas ≤ 64 KiB), and filtered to the run's
  declared tools, because only a declared tool can ever execute. The response frame
  carries the core `tool_calls` back. Model-call budgets and limits are unchanged.
- **Shim (`bridge_shim.py`)**: parses OpenAI `tools` / `tool_calls` / `role: tool`
  messages (null assistant content normalized to `""`), maps stop reason `tool_use` to
  `finish_reason: "tool_calls"`, and emits the SSE replay. Every existing protection
  (127.0.0.1 binding, Bearer key, Host check, body and request caps, per-request
  timeout) applies to all of it.

### Representability boundary (reported, not redesigned)

The core `ModelRequest` carries tool traffic only *after* the current user text
(`turns`), and its `history` is text-only. A conversation in which a completed tool
exchange is followed by a later user message (multi-turn agent memory) therefore cannot
be represented without a core change. Per this ADR's rules the Worker refuses such
requests (`model_request_rejected`) instead of silently rewriting history, and the
smallest additive alternative — letting `ConversationTurn` include a user turn, or
giving `HistoricalMessage` optional tool traffic — is left for a future ADR.

## Consequences

- Unmodified OpenAI-compatible SDKs can run a full tool loop through NervOS mediation.
- The MCP endpoint, credentials, containment, grants, audit, and limits are unchanged.
- Streaming is cosmetic replay, not incremental generation; clients see the same
  latency as non-streaming.

## Verification

- `apps/worker/tests/integration/test_real_e2e_shim_tools_stream.py`: real-socket
  tool-calling loop, streamed text with usage, streamed tool call, and the 400/413
  rejections, asserting rejected requests never reach the wire.
- `apps/worker/tests/unit/test_model_tool_mediation.py`: the Worker mapping and its
  refusal of the unrepresentable conversation shape.
- `apps/worker/tests/unit/test_granted_tools_payload.py`: the initialize frame now
  lists exactly the run's granted tools (ADR 0041 M3's known gap, closed here).
- `packages/nervos-sdk/tests/test_public_contracts.py`: the new optional SDK fields.
- `packages/nervos-package-host/tests/test_wire_model_tools.py`: frame roundtrips and
  old-frame compatibility.
