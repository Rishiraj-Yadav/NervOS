# NervOS Runtime

## Status

Stage B trusted-agent runtime proof. B1 implements provider-neutral Agent Definition, Agent Instance, and Run domain/persistence. B2 implements the internal one-shot Anthropic execution path described below. B3 exposes that path as an authenticated, owner-scoped HTTP API with a minimal trusted Chat dashboard interaction. B4 second-provider portability and final Stage B acceptance remain unimplemented.

## Stage B trusted-agent proof

The implemented B2 execution path is:

```text
trusted owner + explicit Agent Instance intent
  -> exact nervos.chat@1 behavior and provider preflight
  -> committed immutable created Run
  -> committed running Run
  -> no database session/transaction
  -> one bounded ModelCompletion.complete invocation
  -> one Anthropic messages.create invocation (max_retries=0)
  -> committed succeeded or failed Run
```

B2 was an internal application/runtime proof, not a public Chat feature: no Agent, Run, or Chat API route and no frontend UI existed in B2. B3 layers the authenticated HTTP resources and the minimal Chat dashboard interaction on top of the same unchanged execution path.

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

### Provider credential in a B3 run

B3 adds no route to set, read, rotate, or delete a provider credential and no credential field in any schema. When `anthropic` is known but the API process holds no credential, the preflight rejects before Run creation with `409 model_provider_unavailable` — making zero model calls and creating no Run. The UI surfaces that as an inline error.

## Definition, instance, and snapshot

The built-in definition remains exactly `("nervos.chat", "1")`; resolution has no latest fallback. Its trusted behavior owns the fixed instruction:

```text
You are a helpful assistant. Answer the user's request directly and accurately.
```

A meaningful instruction or behavior change requires a new exact definition version. The generic `AgentDefinition` does not contain a system instruction.

An explicitly created, owner-scoped Agent Instance stores canonical provider ID and opaque operator-configured model ID. A Run snapshots the exact definition, provider/model, input, and effective limits. Execution uses only that immutable committed snapshot; later instance edits or disable do not alter or cancel it.

## Model boundary and Anthropic adapter

The B1 application-owned async `ModelCompletion` port is unchanged. `ModelRequest` carries only system instruction, user text, opaque model, maximum output tokens, and timeout. `ModelResponse` carries normalized text, provider/model identity, optional finish outcome, and optional normalized usage. There is no generic message history, tool, streaming, media, structured output, credential, or SDK type in the port.

B2 activates `packages/nervos-models` and implements exactly one provider, canonical ID `anthropic`, using the official asynchronous Anthropic Python SDK and non-streaming Messages API. Anthropic SDK imports remain in `nervos-models`; core domain/application remain provider-SDK-free.

The adapter issues one `messages.create` call and the trusted handler invokes `ModelCompletion.complete` once. The SDK is constructed with `max_retries=0`; NervOS performs no retry, fallback, continuation, or malformed-output second request. This does not claim control over TCP/proxy retransmission.

### Content and finish policy

The adapter concatenates only user-visible `text` blocks in provider order without adding separators or trimming/normalizing. `thinking` and `redacted_thinking` are ignored and never exposed, persisted, or logged. Tool/server-tool/MCP-use and unknown blocks fail closed.

Only Anthropic `end_turn` produces canonical successful finish reason `stop`. `max_tokens` and context-window exhaustion become `model_output_incomplete`; refusal becomes `model_refused`; stop-sequence, tool-use, pause-turn, absent, and unknown reasons are invalid in B2. No continuation request is made.

Reported trustworthy input/output token counts are retained independently. `total_tokens` remains NULL; NervOS does not derive or estimate it or store cache/reasoning/provider-specific breakdowns.

## Provider and secret boundary

B2 reads exactly `ANTHROPIC_API_KEY` through an explicit optional `SecretStr` settings alias. It does not read `NERVOS_ANTHROPIC_API_KEY`, automatically load `.env`, persist a credential/reference, or require a credential to construct Settings/start the API/use Stage A behavior.

Provider resolution distinguishes unknown, known-unavailable, and configured. Anthropic is always known; without a process credential it is unavailable, so preflight rejects before Run creation and performs zero model calls. No provider request occurs during import or API startup.

Credential flow is process environment -> secret-aware API composition -> Anthropic client only. Credentials, authorization headers, raw errors, prompts, answers, thinking, and redacted thinking are not logged. During an explicitly requested real execution, the fixed system instruction and user prompt leave the local machine for Anthropic, and only validated user-visible output may be persisted in a succeeded Run.

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

All automatic tests use deterministic model/client doubles and require no provider credential, network, quota, or paid access. B3's API integration tests install the double by replacing the provider catalog on `app.state`. The browser journey runs against a real API subprocess whose supervisor launches a test-only ASGI factory outside the shipped packages and explicitly removes `ANTHROPIC_API_KEY` from the child environment, so automated E2E cannot consume a real credential even when the operator's shell has one. A failed injection fails the run loudly rather than falling back to Anthropic. `scripts/manual_anthropic_b2_proof.py` is operator-only, requires a new explicit non-default disposable DB and current operator model, constructs a proof user/instance, and executes the full coordinator. It must be separately authorized before running and never prints credential, prompt, answer, hidden reasoning, or raw response/error.

Current live status:

```text
REAL PROVIDER PROOF NOT EXECUTED — CREDENTIAL/ACCESS UNAVAILABLE
```

## Future durable runtime

B3 adds authenticated HTTP resources and minimal dashboard interaction. Stage C adds Jobs, Attempts, workers, claims/leases, retries, durable cancellation, recovery/reconciliation, stale-run handling, and events. Later stages add tools, scheduling, conversations/memory, packages, security isolation, Marketplace, and multi-agent behavior.

### No recovery promise

Stage C owns recovery. If the browser refreshes or navigates away during an awaited Run, the HTTP connection is lost and the Run may remain `running`. On reload the UI rebuilds history purely from persisted Runs and displays a stranded `running` Run as `running`; it neither presents it as actively progressing nor auto-retries it. B3 implements no recovery, no reconciliation, and no polling.
