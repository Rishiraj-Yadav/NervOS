# NervOS Sessions

## Two distinct session types

NervOS uses "session" in two separate domains. Never merge the concepts.

## Authentication session

Stage A implements browser/dashboard login sessions.

Implemented A3 behavior:

- cryptographically random 256-bit opaque token
- token sent only in the host-only `nervos_session` HttpOnly cookie
- only the 32-byte SHA-256 digest stored in DB
- seven-day absolute server-side expiration with no sliding renewal
- revocation on current-session logout
- `SameSite=Lax`, `Path=/`, explicit Max-Age/Expires
- Secure in production and whenever the configured origin is HTTPS
- no authentication token in localStorage/sessionStorage, URLs, or JSON bodies

See `docs/authentication.md` and ADR 0006 for the exact API, Origin, cookie, process-local Argon2 resource bound, and error contracts. The 429 resource bound does not replace reverse-proxy or network rate limiting for externally exposed deployments.

## Agent conversation session

**Implemented in Stage F (F1–F5).** A Conversation is a durable, owner-scoped sequence of Turns between a user and one AgentInstance.

```text
Research Agent
  Conversation "Solar paper"
    - Turn 1: user message → Run 1 → assistant message
    - Turn 2: user message → Run 2 → assistant message

  Conversation "Battery research"
    - Turn 1: user message → Run 3 → assistant message
```

A Conversation belongs to exactly one `(owner_user_id, agent_instance_id)`. One AgentInstance may have many Conversations. Another owner or AgentInstance cannot read it unless a future explicit sharing mechanism grants access. Automated schedule/webhook/event Runs may have `conversation_id = NULL`.

## Conversation != Run

One Conversation contains many Turns; one Turn may link to one or more Runs (initial + retries). A Run is one execution.

Background scheduled runs have `conversation_id = NULL` because they are not conversational.

## Conversation fields (implemented)

- id
- owner_user_id
- agent_instance_id
- title
- status (active, archived, deleted)
- created_at, updated_at
- archived_at, deleted_at

Turns include sequence, status (pending, running, succeeded, failed, cancelled, ambiguous), authoritative_run_id, and at most one ASSISTANT message.

Messages include role (USER/ASSISTANT), content, and created_at. Ordering is by `(turn.sequence, role_order)` where USER=0, ASSISTANT=1.

## Context strategy

Stage F uses deterministic context assembly: conversation compaction (non-model) plus scoped memory injection, bounded by frozen limits (≤20 recent messages, ≤8000 bytes/4000 code points input, ≤4000 injected memory bytes). No model summaries.

## Multi-agent note

An agent handoff does not require merging two conversation sessions. Use explicit task/handoff data or shared workspace memory (deferred to Stage J).
