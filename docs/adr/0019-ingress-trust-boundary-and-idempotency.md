# ADR 0019 — Stage E Ingress Trust Boundary, Idempotency, and Internal Events

Status: Accepted for E0 architecture freeze

This ADR freezes **who may cause a Run to exist**, and what may never be believed about what they sent.
The scheduling semantics are ADR 0018; the single acceptance path every Run travels is ADR 0020.

## Context

Stages A–D had one ingress: an authenticated, Origin-checked, owner-scoped browser API. Every input
that reached the system came from a user the system already trusted, through a boundary that already
existed.

Stage E opens two new ones.

**A webhook is a server-to-server request that has no browser Origin.** The repository's existing
request boundary enforces an exact `Origin` match on every non-safe `/api/v1` request — deliberately,
as CSRF protection for a cookie-authenticated browser API. A genuine webhook from a third-party service
sends no `Origin` header at all. The obvious cheap fix — exempting some paths from the Origin check —
would weaken the exact protection that boundary exists to provide, for every path it covers.

**An internal event is a publication from something that is not a browser at all.** Stage K will want a
device gateway to publish sensor readings; Stage J will want an agent to publish an explicit signal.
Neither can be modelled as an HTTP request without baking a transport into what is really a value.

Two more facts shape the design.

**NervOS has no secret manager and no rate limiter.** Persistent secret management belongs to Stage H.
There is no general request-rate limiter anywhere in the repository. Stage E must therefore design
authentication and abuse control that are honest about what they do and do not provide, rather than
borrowing a mechanism that does not exist.

**The repository already has a machine-token convention.** Session tokens are 256-bit random values
whose SHA-256 digest is stored and looked up. That is the pattern a webhook secret follows — not
password hashing, which exists because human-chosen secrets have low entropy and machine tokens do not.

## Decision

### The webhook namespace is a sibling of the API, not an exemption inside it

```
/hooks/v1/{public_id}          server-to-server delivery, secret-authenticated
/api/v1/...                    the existing authenticated browser API, unchanged
```

This is clean rather than a compromise because of a source fact: the existing request boundary scopes
its Origin check to paths beginning `/api/v1`. A path under `/hooks/` therefore never enters that
branch, so **no Origin or CSRF check is weakened, exempted, or special-cased anywhere.** The browser
API's protection is exactly what it was.

The namespace is versioned (`/hooks/v1`) so a future payload-protocol revision has a stable home
without touching `/api/v1` semantics.

The two surfaces stay firmly distinct:

| | Management | Delivery |
|---|---|---|
| Path | `/api/v1/triggers/...` | `/hooks/v1/{public_id}` |
| Authentication | session cookie + Origin | webhook secret |
| Who calls it | the owning user's browser | a remote service |

A **login session cookie is never a webhook credential**, and a webhook secret never authorizes a
management operation.

### Locator and credential are separate things

A webhook trigger has two independent values:

| Value | Purpose | Entropy | Visible in |
|---|---|---|---|
| `public_id` | **locator** — which endpoint was called | ~128 bits, opaque, URL-safe | URLs, logs, the management API |
| secret | **credential** — proves the caller may use it | 256 bits, opaque, URL-safe | **only** the one creation/rotation response |

They are generated separately and neither is derived from the other. The public locator may appear in
logs and URLs precisely because it authenticates nothing; the secret never appears anywhere except the
single response that returned it.

Using one value for both would mean the endpoint identity — which is inherently visible in every
request line — was also the credential. `public_id` being unguessable is an onboarding property, not an
authentication property, and the two are not conflated.

### Authentication: lookup by locator, then a constant-time comparison

Verification proceeds in a fixed order:

1. Look up the webhook `TriggerDefinition` by `public_id`.
2. Compute `SHA-256(candidate_secret)`.
3. Compare the candidate digest with the stored digest using **`hmac.compare_digest`**.
4. Resolve the trigger's authority (enabled, owner, target) **inside the materialization transaction**.

For an unknown `public_id`, the comparison is still performed, against a **fixed dummy 32-byte digest**,
so the work and the timing of the two paths are the same. Both an unknown locator and a wrong secret
produce the **same public response**, so the endpoint cannot be probed for which endpoints exist.

Two consequences worth stating plainly:

- **`secret_digest` is not the authentication authority and carries no unique constraint.** The locator
  is `public_id`; the digest is compared, not searched for. Making the digest a lookup key would turn
  a credential into an identifier and would let a digest collision become an authorization event.
- **A timing side channel is closed by construction rather than by argument.** Storing a 256-bit random
  secret means a digest comparison leaks nothing an attacker could exploit, but the comparison is
  constant-time anyway, because it costs nothing to be right about this.

The comparison above is *authentication*. It is **not** authorization: step 4 is where authority is
resolved, and it happens **inside** the transaction that will act on it.

### The call-time authority rule

**Authority is never derived from an object read before the transaction that acts on it.**

The lookup in step 1 produces a candidate, not a decision. Inside the materialization transaction the
trigger row is re-read, and the transaction proceeds only if the trigger is still enabled, still owned
by the same user, still targeting an Agent Instance that is still owned and still enabled, and still
carrying the same secret digest.

| Interleaving | Winner | Result |
|---|---|---|
| disable commits first | disable | no Run; the delivery is refused |
| delivery commits first | delivery | the Run exists; the later disable does **not** cancel it |
| secret rotation commits first | rotation | no Run; authentication fails |

This mirrors the capability boundary Stage D froze: an authorization decision is made at the moment of
use, in the same transaction as the effect, and a stale in-memory object has no authority at all.

### Secret generation, storage, and rotation

| Step | Decision |
|---|---|
| Generation | `secrets.token_urlsafe(32)` → a 43-character URL-safe string, 256 bits of source entropy |
| Storage | **SHA-256 digest only**, 32 bytes, never plaintext, never reversible |
| Plaintext lifetime | returned **once**, from the creation or the rotation response, and never persisted |
| Logging | never — not the secret, not the `Authorization` header, not the body that carried it |
| Rotation | an explicit action that atomically replaces the digest; the old secret is **immediately** invalid |
| Recovery | **none, by design.** A lost secret is rotated, never recovered. |
| Password-style KDF | **not used.** Argon2id exists because human-chosen passwords have low entropy and need a work factor; a 256-bit machine token does not, and a memory-hard KDF on a per-delivery ingress path would add a denial-of-service lever where none should exist. |

Rotation does not change `public_id`: the endpoint stays the same and only the credential changes.

### Request and body contract

| Property | Value |
|---|---|
| Method | `POST` only |
| Content type | `application/json` only — a single JSON **object** |
| Maximum body | 65 536 bytes, enforced **before** parsing and before any database work |
| Depth | bounded to the canonical JSON depth the tool schema already uses |
| Malformed JSON | refused with a static code; never a stack trace, never the parser's message |
| Raw body in logs | never |
| Raw body persisted | **never** — only a digest and a byte count reach the occurrence row |

The bound is applied to the ingress namespace specifically. No global change is made to the
`/api/v1` body bounds.

### The payload is data, and only data

The model-visible input is composed as:

```
final input_text
  = the operator's own trigger input        trusted, authored at save time, validated then
  + a delimited, canonical rendering of the delivered payload   untrusted, attacker-controlled
```

Rules, frozen:

1. **The operator's text comes first** and is the only part a human wrote. Instructions live there.
2. **The payload is rendered as canonical JSON** — sorted keys, no insignificant whitespace, bounded
   depth and bytes — so two semantically identical deliveries produce byte-identical blocks.
3. **It is explicitly delimited and labelled as untrusted event data**, so a reviewer can see the
   boundary in the stored Run and the model can distinguish instruction from data.
4. **The composed result must satisfy the target Run's own input bounds.** If it cannot, the occurrence
   is skipped and **no Run is created**. Truncation is never used as a bound: it can cut the operator's
   instruction in half while keeping the payload.
5. **The system instruction is never composed from payload.** It remains the frozen trusted-chat
   constant. There is no code path in which external text could reach a system instruction, an Agent
   instruction, a tool description, or a permission policy — the guarantee is structural, not a matter
   of careful string handling.
6. **No new chat architecture.** The result is an ordinary Run input; a Run created this way is
   indistinguishable from a manual one downstream.

This is the milestone's central injection boundary, and it is the reason the freeze is phrased as "the
payload may only ever be data" rather than as a list of forbidden characters.

### Idempotency — exact, and deliberately incomplete

External systems retry. The behaviour is frozen, including where it gives no guarantee:

| Case | Behaviour |
|---|---|
| caller supplies `Idempotency-Key` | unique identity `(trigger_definition_id, idempotency_key)` |
| same key delivered twice | **find the existing occurrence and return its outcome**; **no second occurrence row, no second Run** |
| duplicate of a skipped original | returns the same skip outcome; still no Run |
| no `Idempotency-Key` | each delivery is a **distinct** occurrence and **may create a distinct Run**, even if the body is byte-identical |
| content-digest deduplication | **explicitly refused.** Two identical payloads can be two legitimately distinct events; inferring identity from content would silently drop real events |
| key format | bounded length, printable, non-empty; a malformed key is refused |

**Exactly-once inbound delivery is NOT claimed.** A sender that retries without a key gets a second
Run, and that is the truthful behaviour rather than a bug to be papered over with content hashing.

### Responses are bounded, and indistinguishable where probing matters

- Success returns an accepted status with the occurrence id and, when materialization completed, the
  Run id, plus a `duplicate` indication when the identity was already known.
- **No Job id, Attempt id, claim token, lease, worker identity, internal error, or raw payload** is ever
  returned.
- **Unknown locator and wrong secret are indistinguishable.** A caller that does not hold the secret
  learns nothing about which endpoints exist.
- A disabled trigger, a malformed body, an over-size body and an unavailable Agent each return a static,
  safe code with a static message.

The `duplicate` fact is **ephemeral, in the response only** — see ADR 0018. It is never persisted as an
occurrence status, because occurrence history records materialized occurrences rather than every
delivery attempt. A history that logged each retry would turn one event into many.

### Residual abuse boundary — stated, not implied

**There is no rate limiter in Stage E, and none is invented.** Protection comes from properties that
already exist or are frozen above:

| Mechanism | Bound |
|---|---|
| Endpoint identity | unguessable ~128-bit `public_id` |
| Credential | 256-bit secret, constant-time verified |
| Body | 65 536 bytes, refused before parsing |
| Database work per delivery | one indexed lookup, at most one short transaction |
| Fanout | **none on this path** — one delivery reaches one trigger, one occurrence, one Run |
| Volume that becomes Runs | refused by **Stage C's existing admission backpressure**, not by a new limiter |

**Recorded as a residual limitation:** per-hook request-rate limiting and per-owner ingress quotas are
**deferred**. The mitigation available to an operator today is a reverse proxy in front of the ingress.
Stage H owns real operational hardening. The plan and the docs state this rather than implying a
protection that does not exist.

### The internal event model is a value, not a request

```
EventEnvelope
    event_id          caller-supplied, opaque, bounded
    owner_user_id     authoritative, supplied by the ingress — never read from the payload
    event_type        lowercase dotted name, bounded
    occurred_at       aware UTC instant
    source            descriptive provenance only
    payload           untrusted, bounded, canonical
```

**`EventEnvelope` is not HTTP-specific and not webhook-specific.** It contains no request, response,
header, route or transport type. That is the entire Stage K compatibility story and it costs nothing
today except the decision not to model events as HTTP requests: a device gateway resolves its owner
from its own registration, constructs this value, and publishes it through the same ingress. The
scheduler, the occurrence model, the identity scheme and the dedupe constraints do not change.

**`source` is descriptive provenance, not authorization.** It cannot confer authority. `owner_user_id`
is supplied by the ingress boundary that received the publication, never read from a
client-controlled field, and there is no `trusted` flag in the model at all.

### Event naming

| Rule | Value |
|---|---|
| Case and separator | lowercase, dotted |
| Grammar | dotted segments, each beginning with a letter |
| Maximum length | 64 characters |
| Reserved namespace | **`nervos.*` is reserved** and rejected from user publication in Stage E |
| Wildcards | none |

`nervos.*` is reserved and **empty** — Stage E emits no system event. Reserving it costs one validation
rule and preserves the option of a first-party event producer later without a migration or a collision.

### Event matching is exact, and stays exact

Matching requires **exact `event_type` equality**, the **same owner**, `enabled = 1`, and
`kind = 'event'`.

**Not supported, and not planned for Stage E:** JSONPath, JQ, CEL, regular expressions over the
payload, arbitrary predicates, user-supplied code, attribute filters, content matching, priority,
throttling, or chaining.

An event-routing DSL is the most attractive scope creep available in this milestone — a language with
its own escaping rules, injection surface, performance characteristics and security review. Exact
matching covers everything the stage's outcome requires, and filtered subscriptions can later be added
behind the same ingress without changing the scheduler, the occurrence model or the identity scheme.

### Event identity, owner scope, dedupe and fanout

| Aspect | Decision |
|---|---|
| Identity | caller-supplied `event_id`, opaque, bounded. The publisher is responsible for reusing it for the same logical event. |
| Dedupe | `(trigger_definition_id, event_id)` unique. Republishing the same id to the same trigger creates nothing. |
| Same id, different trigger | legitimately a distinct occurrence — identity is **per-subscription**, not global |
| Owner scope | matched in the same predicate as the type. **User A's event can never fire User B's trigger.** |
| Global / system events | **out.** No broadcast case, no system owner. |
| Fanout | **at most 32 matching triggers per publication** |
| Over the bound | **the publication is refused, and nothing is delivered.** Not a partial subset. |
| Ordering within fanout | deterministic, by trigger id |

**Failing closed above the bound is deliberate.** Silently delivering an arbitrary subset would make
"why did only some of my triggers fire?" unanswerable, and the subset would be chosen by an ordering
nobody specified. A refused publication is a legible outcome; a silently truncated one is not.

The bound also keeps the publication transaction short, which the single-write-lock SQLite policy
requires.

### Events are not durably stored as events

**Stage E has no separate event table.** The occurrence rows are the durable record; an `event_id`
appears on them as an identity, and a payload digest and byte count may accompany it.

| Consideration | Reasoning |
|---|---|
| Requirements | "republishing the same event id must not create another Run" is a unique constraint on occurrences; nothing else needs an event row |
| Replay | **deferred, and made structurally impossible** — there is no table to iterate |
| Privacy | a payload's only durable home is the Run's own immutable input snapshot, which already has an owner, bounds and validation |
| Authority | an event log would become a second account of what happened |
| Growth | one row per *matched* occurrence, strictly smaller than one row per *published* event |

**The accepted cost, stated:** an event that matches no trigger leaves **no durable trace**. An operator
debugging a non-firing event has logs, not a query. Adding a bounded, opt-in event log later is a
migration that does not disturb the occurrence model.

### No replay

Stage E adds no "replay event", "re-run all missed events", or "reprocess historical webhook"
capability. A user who wants another Run creates one. Durable history is not automatically a replay
engine, and a table that exists only so it can be replayed is a table that will eventually be replayed.

### No automatic Run → Event recursion

Stage E **never** converts a Run Event into a trigger input. Otherwise

```
trigger → Run → run.succeeded → trigger → Run → …
```

would be an accidental automation loop with no operator intent, no depth bound and no visible cause.
Trigger execution terminates at Run creation. A future stage that wants an agent to trigger another
agent must do it through an **explicit** publication, not an implicit bridge.

### Credential authority is not preflighted

Stage E does **not** preflight or snapshot Worker provider credentials. A triggered Run behaves exactly
like a manual Run: provider credential absence remains a Worker execution concern. Likewise, tool
grants are checked live by Stage D when the Run executes. **A trigger never bypasses tool grants
because it is "trusted automation".**

A webhook secret authenticates *who may fire this trigger*. It grants no capability, authorizes no
tool, and does not modify a grant cutoff. It is **not** Stage H approval.

### Security logging

Stage E logging never contains a webhook secret, an `Authorization` header, a raw webhook or event
payload, a provider credential, or a tool credential. Safe log fields are identifiers, kinds, static
codes and bounded timestamps.

## Consequences

**The browser API's protection is untouched.** The webhook ingress is a sibling namespace rather than an
exemption, so CSRF protection for the cookie-authenticated API is exactly what it was and no path had to
be weakened to add a second ingress.

**Two credentials now exist where one did, and they are separately auditable.** A leaked webhook secret
compromises exactly one endpoint's ability to fire one trigger; it does not read data, does not
authorize tools, and does not touch the browser API.

**Some senders will get duplicate Runs.** A sender that retries without an idempotency key creates
another occurrence and another Run. That is documented behaviour, not a defect, and it is the honest
alternative to content-hash deduplication that would silently drop genuinely distinct events.

**Unmatched events are invisible.** The observability cost is real and accepted rather than paid for
with an event log that would become a second source of truth.

**A determined caller can still send volume.** There is no rate limiter. The mitigations are
unguessable identity, secret authentication, body bounds and bounded work; per-hook rate limiting and
per-owner quotas belong to Stage H or to an operator's reverse proxy, and that is stated rather than
implied away.

**The event ingress is deliberately under-powered today.** Exact-match, owner-scoped, no filters, no
replay. That is what makes it safe to hand a device gateway later: the ingress is already a value
boundary rather than a transport, so adding a publisher does not change the architecture.
