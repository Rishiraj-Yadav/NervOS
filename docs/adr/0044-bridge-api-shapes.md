# ADR 0044 — Bridge API Shapes: Anthropic Messages, OpenAI Responses, Gemini

**Status: ACCEPTED** — Implemented per the Owner decisions below.

## Context

ADR 0041 gave the generic process agent bridge an OpenAI Chat Completions endpoint, and
ADR 0042 added function calling and SSE replay to it. SDKs that speak other dialects —
Anthropic's Messages API, OpenAI's Responses API, Google's Gemini API — still cannot run
unmodified against the shim, so "any SDK" really meant "any OpenAI-Chat SDK".

## Decision

The shim serves three more API shapes, each converting to the **same** internal
`model_request`/`model_response` wire path the chat route uses. No Worker, SDK-type,
wire, or core change is involved: routes are translation layers inside
`bridge_shim.py` around the shared `_request_model` seam introduced by the ADR 0042
refactor.

### Owner decisions

1. **Limits as amended by ADR 0045**: forced `tool_choice` (required and named) is now
   honored on every route, and multi-turn conversations with a tool exchange before a
   later user message are carried in order; images, audio, files, embeddings, and
   provider-hosted tools still get the vendor-style 400; streaming is **replay** of
   the complete result in the vendor's SSE format, never incremental generation.
2. **No new shipped third-party dependencies**; no live Linux/sandbox/provider calls.
3. **Every protection applies to every route**: 127.0.0.1 binding, Host header check,
   per-run key auth, 1 MiB body cap, request cap, per-request timeout. Auth accepts
   `Authorization: Bearer` everywhere, plus `x-api-key` (Anthropic) and `x-goog-api-key`
   or `?key=` (Gemini). Errors carry the vendor's own envelope (Anthropic
   `{"type":"error","error":{...}}`, OpenAI `{"error":{...}}`, Gemini
   `{"error":{code,message,status}}`).

### Routes

| Route | Vendor shape |
|---|---|
| `POST /v1/messages` | Anthropic Messages: `system` string/text blocks, text content, `tool_use`/`tool_result` blocks, `tools` with `input_schema`, `max_tokens` required, `anthropic-version` required (value ignored), usage, `stop_reason` mapping (`end_turn`/`tool_use`/`max_tokens`), and the documented stream sequence (`message_start`, `content_block_start`, `content_block_delta` with `input_json_delta` for tool arguments, `content_block_stop`, `message_delta`, `message_stop`). |
| `POST /v1/responses` | OpenAI Responses: `input` as string or item list (`message`, `function_call`, `function_call_output`; `reasoning` items accepted and dropped), `instructions`, flat function tools, response object (`id`, `object: "response"`, `status`, `output` items, `output_text`, `usage`), stream sequence (`response.created`, `response.output_item.added`, `response.output_text.delta/done`, `response.function_call_arguments.delta/done`, `response.output_item.done`, `response.completed`). `store` is accepted and ignored; `previous_response_id`, hosted tools, files and images are refused with 400. |
| `POST /v1beta/models/{model}:generateContent` | Gemini: `contents` with text/`functionCall`/`functionResponse` parts, `systemInstruction`, `tools.functionDeclarations`, `toolConfig` (AUTO/NONE), `generationConfig`, `usageMetadata`, `finishReason` mapping (`STOP`/`MAX_TOKENS`). Gemini addresses calls by name, so the function name doubles as the wire call id. |
| `POST /v1beta/models/{model}:streamGenerateContent?alt=sse` | The same request shape replayed as Gemini SSE chunks (`data:` lines; content chunk(s), then a closing chunk carrying only `finishReason` and `usageMetadata`). `alt` values other than `sse` are refused. |
| `GET /v1/models` | OpenAI model listing. The host is never told the run's model on the wire, so the route lists the model the child used most recently, falling back to `nervos-model`. |
| `GET /v1beta/models` | Gemini model listing with the same fallback rule. |

### Environment

`process_agent_environment(origin, token)` (package host, `__main__.py`) builds the
child's variables: `OPENAI_BASE_URL` (+`/v1`), `ANTHROPIC_BASE_URL` (bare origin),
`GOOGLE_GEMINI_BASE_URL` (bare origin), `NERVOS_MCP_URL` (+`/mcp`), and the per-run key
in `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, and `GOOGLE_API_KEY`.

## Consequences

- Unmodified Anthropic, OpenAI (Chat or Responses), and Gemini SDKs can drive an agent
  loop through NervOS mediation, grants, audit, and containment.
- The Gemini call-id synthesis means two parallel calls to the same function name are
  indistinguishable on the wire; the first matching name is used (Gemini itself has no
  call ids).
- Discovery routes are advisory: they reflect the last model used, not an allowlist
  (the wire carries no per-run model restriction to the host).

## Verification

- `apps/worker/tests/integration/test_real_e2e_shim_vendor_routes.py`: per route, a
  real-socket tool-calling loop, streamed text parsed to the vendor's end event, a
  streamed tool call reassembled, vendor-shaped 400/401 rejections that never reach
  the wire, both discovery listings, and the child environment contract — 15 tests.
- `packages/nervos-package-host/tests/test_process_agent_environment.py`: the env
  builder itself.
- All pre-existing chat/MCP/SDK suites unchanged and green.
