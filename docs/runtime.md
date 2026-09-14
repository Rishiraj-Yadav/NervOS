# NervOS Runtime

## Status

Stage B trusted-agent runtime proof is implemented. B1 provides Agent Instance and Run domain/persistence, B2 the internal one-shot Anthropic path, B3 the authenticated API and trusted Chat dashboard, and B4 OpenAI Responses portability through the same provider-neutral path.

## Stage B trusted-agent proof

The implemented execution path is:

```text
trusted owner + explicit Agent Instance intent
  -> exact nervos.chat@1 behavior and provider preflight
  -> committed immutable created Run
  -> committed running Run
  -> no database session/transaction
  -> one bounded ModelCompletion.complete invocation
  -> one SDK request on the Run's snapshotted provider (max_retries=0)
  -> committed succeeded or failed Run
```

The concrete request is one Anthropic `messages.create` or one OpenAI `responses.create`, selected solely by the immutable Run provider snapshot.

The B2 subset of this path was an internal application/runtime proof, not a public Chat feature: no Agent, Run, or Chat API route and no frontend UI existed in B2. B3 layers the authenticated HTTP resources and the minimal Chat dashboard interaction on top of the same unchanged execution path, and B4 adds the second adapter behind the same port.

## B3 HTTP surface and dashboard interaction

B3 adds no execution capability. It exposes the already-proven B2 path to an authenticated local user:

```text
authenticated browser
  -> NervOS API (owner-scoped, exact-Origin, session cookie)
  -> existing RunCoordinator.execute(...)
  -> persisted terminal Run (succeeded | failed)
  -> browser renders the persisted Run
```

The resource family is exactly:

```text
GET    /api/v1/agent-instances
POST   /api/v1/agent-instances
GET    /api/v1/agent-instances/{agent_instance_id}
PATCH  /api/v1/agent-instances/{agent_instance_id}
POST   /api/v1/agent-instances/{agent_instance_id}/runs
GET    /api/v1/agent-instances/{agent_instance_id}/runs
GET    /api/v1/runs/{run_id}
```

`POST /api/v1/agent-instances/{agent_instance_id}/runs` is the **only** way a Run is created; there is no `POST /chat`, no global `POST /runs`, and no second execution path. It is the single async route and it awaits `RunCoordinator.execute` directly.

Boundaries B3 preserves:

- **Ownership.** Identity comes only from the authenticated session. No request or response schema carries `owner_user_id`. A foreign id and a nonexistent id are indistinguishable everywhere: `GET`/`PATCH` on an instance, `GET` on a run, `POST` on an instance's runs, and `GET` on an instance's runs all return the identical 404 body. No response reveals whether a resource exists for someone else.
- **Run-list parent.** `GET /api/v1/agent-instances/{agent_instance_id}/runs` resolves the owned parent before it reads any Run, so an owned instance with no Runs returns `200` with an empty page while a nonexistent or foreign instance returns the same `404 agent_instance_not_found` as every other foreign lookup. Run history can therefore never be used to probe whether another user's instance exists, and "no runs yet" stays distinguishable from "not your instance".
- **Creatable definition.** The B3 API accepts creation only for the exact trusted `nervos.chat@1` pair. Any other key or version is rejected with a safe 422 and creates nothing, so a definition registered by a later stage cannot drift into this surface by accident.
- **Provider configuration.** An unknown provider is rejected on both creation and configuration update. A known-but-unconfigured provider is still accepted at configuration time, because a missing process credential is discovered only at execution.
- **Update shape.** A `PATCH` is exactly one of two shapes — the configuration triple, or `enabled` alone — and maps to exactly one committed application write. Definition identity and owner are immutable.
- **Execution response.** Execution is awaited; there is no queue, job, worker, `202`, polling, retry, or re-execute endpoint. `POST …/runs` returns **201** with the persisted Run, whether it is `succeeded` or `failed`, because the HTTP status describes the resource operation and never the model outcome. Only pre-run rejections and platform failures use an HTTP error status.
- **Usage.** `usage` is `null` exactly when no trustworthy counters were persisted; it is never an all-null object, and `total_tokens` is never derived.
- **Rendering.** Model output is rendered as escaped plain text with newlines preserved. No markdown library, sanitizer, or HTML injection is used.

### One submission is one independent Run

The Chat UI may look conversational, but there is no conversation system in B3:

- One user submission creates **one independent Run**.
- Each Run sends only the fixed `nervos.chat@1` system instruction plus the current submitted text.
- No prior Run's input or output ever becomes model context. There is no `conversation_id`, no `session_id`, no Message record, and no replay.
- Prior Runs are displayed to the human as independent persisted Run records under the wording **"Run history"**, with an explicit disclosure that earlier runs are not sent to the model.

Conversations, sessions, and memory belong to a later stage.

### Provider credential in a Stage B run

No route sets, reads, rotates, or deletes a provider credential, and no schema carries a credential field. When a known provider's process credential is absent, the preflight rejects before Run creation with `409 model_provider_unavailable` — making zero model calls and creating no Run. The UI surfaces that as an inline error. Each provider is independent: an unconfigured `openai` does not affect an `anthropic` execution, and a configured provider is never substituted for an unconfigured one.

## Definition, instance, and snapshot

The built-in definition remains exactly `("nervos.chat", "1")`; resolution has no latest fallback. Its trusted behavior owns the fixed instruction:

```text
You are a helpful assistant. Answer the user's request directly and accurately.
```

A meaningful instruction or behavior change requires a new exact definition version. The generic `AgentDefinition` does not contain a system instruction.

An explicitly created, owner-scoped Agent Instance stores canonical provider ID and opaque operator-configured model ID. A Run snapshots the exact definition, provider/model, input, and effective limits. Execution uses only that immutable committed snapshot; later instance edits or disable do not alter or cancel it.

## Model boundary and provider adapters

The B1 application-owned async `ModelCompletion` port is unchanged. `ModelRequest` carries only system instruction, user text, opaque model, maximum output tokens, and timeout. `ModelResponse` carries normalized text, provider/model identity, optional finish outcome, and optional normalized usage. There is no generic message history, tool, streaming, media, structured output, credential, or SDK type in the port.

`packages/nervos-models` implements exactly two production providers: canonical `anthropic` through the asynchronous Messages API and canonical `openai` through the asynchronous Responses API. Both SDKs remain isolated in `nervos-models`; core, routes, and frontend remain provider-SDK-free. OpenAI requests are stateless, non-streaming and non-background, use `store=False`, and send no conversation, previous response, tools, metadata, or reasoning controls.

Each adapter issues exactly one SDK request and the trusted handler invokes `ModelCompletion.complete` once. Both SDKs are constructed with `max_retries=0`; NervOS performs no retry, fallback, continuation, or malformed-output second request. A failing selected provider is never retried against the other provider. This does not claim control over TCP/proxy retransmission.

Both production client factories also pin the canonical HTTPS API base URL explicitly, `https://api.anthropic.com` and `https://api.openai.com/v1`. Without that pin the SDKs resolve their destination from the ambient `ANTHROPIC_BASE_URL` and `OPENAI_BASE_URL` variables, which could silently send a request intended for the canonical provider to an OpenAI-compatible or proxy endpoint. There is no configuration for a custom or OpenAI-compatible endpoint, and no `Settings` field for one.

The SDKs read the process environment while a client is constructed, so both production client factories also own their authentication and scoping headers. Each declares its credential header explicitly (`Authorization` for OpenAI, `x-api-key` for Anthropic) and omits the headers NervOS does not use (`OpenAI-Organization`, `OpenAI-Project`, and, for Anthropic, `Authorization`). Client default headers are the SDKs' highest-precedence header layer, above each SDK's own credential header and above anything parsed from `ANTHROPIC_CUSTOM_HEADERS` or `OPENAI_CUSTOM_HEADERS`. Authentication therefore comes only from the approved process credential for the selected canonical provider, an ambient custom header or `ANTHROPIC_AUTH_TOKEN` cannot replace or supplement it, and an ambient `OPENAI_ORG_ID` or `OPENAI_PROJECT_ID` cannot silently scope a request. This isolation uses only the SDKs' public constructor arguments and public header-omission sentinel; no process environment variable is ever read or written by NervOS adapter code, and no private SDK internal is used.

The same ambient variables can also carry any *other*, arbitrary header name, and the locked SDKs parse that variable inside client construction. Each factory therefore replaces that custom layer afterwards through the public `with_options(set_default_headers=...)` API. The replacement freezes canonical HTTP basics (`Accept`, `Content-Type`, and the public SDK `user_agent`), public `platform_headers()`, required protocol/telemetry headers (including Anthropic's pinned `anthropic-version`), and NervOS-owned authentication/scoping policy. Every remaining, ambient-only name is removed with the SDK's public `Omit` sentinel. This handles both arbitrary new names and collisions where an ambient value reuses an SDK-owned canonical name. Stage B therefore supports exactly one provider-environment input per provider, its API key: an ambient custom-header variable can neither inject a header into, nor replace the authentication of, nor scope a canonical request. No process environment variable is read or written by NervOS adapter code, no private SDK internal is used, and no HTTP client is replaced.

A credential placed in a `NERVOS_`-prefixed variable is never read. Configurable provider endpoints, organization or project scoping, and custom provider headers are not Stage B features: an ambient `ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL`, `ANTHROPIC_CUSTOM_HEADERS`, or `OPENAI_CUSTOM_HEADERS` value cannot redirect, authenticate, scope, or add a header to a canonical request, and there is no `Settings` field for any of them.

### Content and finish policy

Each adapter concatenates only user-visible text in provider order without adding separators or trimming/normalizing. Anthropic `thinking`/`redacted_thinking` and OpenAI reasoning items are ignored and never exposed, persisted, or logged. Tool, server-tool, MCP-use, function-call, and unknown blocks or output items fail closed in either adapter, and that item check applies to every terminal outcome. For OpenAI, typed `response.output` is inspected directly rather than the convenience aggregate, at most one assistant message is accepted, and any explicit refusal dominates and is never exposed.

Accepted visible text is required only for a completion, so an empty or blank completed response is invalid. A `max_output_tokens` or `content_filter` outcome is still canonical when the provider returns no output item at all, which is the shape a truncated or filtered response actually has; any partial text accompanying such an outcome is discarded rather than exposed.

Anthropic `end_turn` and a completed OpenAI response with accepted visible text produce canonical `stop`. Anthropic `max_tokens`/context exhaustion and OpenAI `incomplete:max_output_tokens` become `model_output_incomplete`. Anthropic refusal and OpenAI explicit refusal or `incomplete:content_filter` become `model_refused`. Stop-sequence, tool-use, pause-turn, and unknown Anthropic reasons; OpenAI `incomplete:max_messages`, `incomplete:steered`, unknown or absent incomplete reasons, `in_progress`, `queued`, `cancelled`, and unknown statuses; and a failed OpenAI response all fail safely. A non-null structured OpenAI `response.error` is never serialized. No raw provider stop string or status is persisted as `finish_reason`, and no continuation request is made.

Reported trustworthy input/output token counts are retained independently. Anthropic's `total_tokens` remains NULL because it is not derived; OpenAI's provider-reported `total_tokens` is retained as reported and never recomputed. NervOS does not estimate or store cache/reasoning/provider-specific breakdowns.

## Provider and secret boundary

Each provider reads exactly one standard process variable through an explicit optional `SecretStr` settings alias: `ANTHROPIC_API_KEY` and `OPENAI_API_KEY`. NervOS does not read the `NERVOS_`-prefixed variants, automatically load `.env`, persist a credential or reference, or require any credential to construct Settings, start the API, or use non-execution behavior. An absent, empty, or whitespace-only value means unavailable.

Provider resolution distinguishes unknown, known-unavailable, and configured. Exactly two canonical identities are known — `anthropic` and `openai` — with no aliases, case folding, dynamic discovery, or provider scanning. An unconfigured provider is unavailable, so preflight rejects before Run creation and performs zero model calls. No provider request occurs during import or API startup.

Credential flow is process environment -> secret-aware API composition -> the selected provider's client only. Credentials, authorization headers, raw errors, prompts, answers, hidden reasoning, and refusal text are not logged. During an explicitly requested real execution, the fixed system instruction and user prompt leave the local machine for the selected provider, and only validated user-visible output may be persisted in a succeeded Run. `store=False` disables OpenAI Responses storage as supported by that API; it is not a claim of zero provider-side retention under every provider, security, or legal policy.

## Limits, timeout, and cancellation

Stage B snapshots 8,000 UTF-8 bytes/4,000 code points input, 32,000 bytes/16,000 code points output, 60,000 ms provider timeout, 1,024 requested output tokens, and one model call. Provider output is never truncated.

The immutable Run timeout is passed explicitly to the SDK request and also wraps the complete handler/provider await through authoritative `asyncio.timeout`. SDK or outer timeout becomes `model_timed_out`; terminal persistence moves the Run to `failed` when available. There is no timed-out state and no retry.

External `asyncio.CancelledError` is distinct: it propagates and is not normalized as timeout/success. B2 does not promise cancellation cleanup; the Run may remain `running`. Process exit/persistence failure can likewise strand `created` or `running`. Stage C owns durable cancellation, retries, and reconciliation.

## Persistence boundaries and failure classes

B2 uses exactly three independent transaction boundaries:

1. create immutable `created` Run, commit, close;
2. conditionally transition `created -> running`, commit, close;
3. await the model with no DB session/transaction/ORM record retained, then conditionally persist `running -> succeeded|failed`, commit, close.

Pre-run configuration/resource rejection creates no Run and makes zero calls. Provider/execution failures after `running` persist a stable safe code and static NervOS-owned message when terminal persistence works. Persistence/platform failures never claim they were recorded. A model answer is never returned or represented as success before the succeeded Run commit; if that commit fails, the answer does not escape.

## Verification and manual proof

All automatic tests use deterministic model/client doubles and require no provider credential, network, quota, or paid access. The API integration tests install doubles by replacing the provider catalog on `app.state`; two-provider tests configure a distinct recording double per canonical ID, which is what makes a routing or fallback defect observable. The browser journey runs against a real API subprocess whose supervisor launches a test-only ASGI factory outside the shipped packages and explicitly removes `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` from the child environment, so automated E2E cannot consume a real credential even when the operator's shell has one. A failed injection fails the run loudly rather than falling back to a real provider. `scripts/manual_anthropic_b2_proof.py` is operator-only, requires a new explicit non-default disposable DB and current operator model, constructs a proof user/instance, and executes the full coordinator. It must be separately authorized before running and never prints credential, prompt, answer, hidden reasoning, or raw response/error.

Current live status:

```text
ANTHROPIC LIVE PROOF — NOT EXECUTED
OPENAI LIVE PROOF — NOT EXECUTED
```

Live cloud proof is optional and separately authorized; it is never a software-acceptance requirement. See [Stage B working demo](stage-b-demo.md) for the deterministic and real-cloud procedures.

## Future durable runtime

B3 adds authenticated HTTP resources and minimal dashboard interaction; B4 adds the second production adapter without changing that surface. Stage C adds Jobs, Attempts, workers, claims/leases, retries, durable cancellation, recovery/reconciliation, stale-run handling, and events. Later stages add tools, scheduling, conversations/memory, packages, security isolation, Marketplace, and multi-agent behavior.

### No recovery promise

Stage C owns recovery. If the browser refreshes or navigates away during an awaited Run, the HTTP connection is lost and the Run may remain `running`. On reload the UI rebuilds history purely from persisted Runs and displays a stranded `running` Run as `running`; it neither presents it as actively progressing nor auto-retries it. B3 implements no recovery, no reconciliation, and no polling.
