# NervOS Runtime

## Status

Stage B trusted-agent runtime proof. B1 implements provider-neutral Agent Definition, Agent Instance, and Run domain/persistence. B2 now implements the internal one-shot Anthropic execution path described below. B3 Agent/Run/Chat HTTP resources and dashboard interaction, and B4 second-provider portability, remain unimplemented.

## Stage B trusted-agent proof

The implemented B2 path is:

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

This is an internal application/runtime proof, not a public Chat feature. No Agent, Run, or Chat API route or frontend UI exists in B2.

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

All automatic tests use deterministic model/client doubles and require no provider credential, network, quota, or paid access. `scripts/manual_anthropic_b2_proof.py` is operator-only, requires a new explicit non-default disposable DB and current operator model, constructs a proof user/instance, and executes the full coordinator. It must be separately authorized before running and never prints credential, prompt, answer, hidden reasoning, or raw response/error.

Current live status:

```text
REAL PROVIDER PROOF NOT EXECUTED — CREDENTIAL/ACCESS UNAVAILABLE
```

## Future durable runtime

B3 adds authenticated HTTP resources and minimal dashboard interaction. Stage C adds Jobs, Attempts, workers, claims/leases, retries, durable cancellation, recovery/reconciliation, stale-run handling, and events. Later stages add tools, scheduling, conversations/memory, packages, security isolation, Marketplace, and multi-agent behavior. None is part of B2.
