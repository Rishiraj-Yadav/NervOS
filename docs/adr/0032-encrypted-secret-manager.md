# ADR 0032 — Encrypted Secret Manager

Status: Accepted for Stage H implementation (H1)

## Context

NervOS accumulates credentials it must protect: connector tokens (H2), webhook capability secrets
(today hashed, still a secret at creation), and future provider-adjacent credentials. Stage D
deliberately shipped **no** secret store — ADR 0016 froze credential aliases as operator
environment configuration and deferred "a real secret manager" to Stage H. Persisting any secret
before an encrypted store existed was called out as the exact failure to avoid.

## Decision

### Encryption design (reviewed, standard, no invented cryptography)

- **AES-256-GCM** envelope encryption via the maintained `cryptography` library already in the
  dependency tree (declared directly in `nervos-core` for Stage G Ed25519).
- Each secret row stores: `key_version`, a fresh random 12-byte nonce, and `ciphertext||tag`.
  Nonces are never reused with the same key; the GCM tag authenticates the row's non-secret
  metadata binding (secret id, key version, nonce) as AAD, so swapping ciphertext between rows
  fails authentication.
- The **master key never enters the database or the source tree**. It is read at process start
  from the file named by `NERVOS_SECRETS_KEY_FILE` (operator-owned, 32-byte base64, mode 0600 on
  POSIX). The API process requires it for secret writes; the Worker requires it for resolution.
- **Fail closed:** a missing, unreadable, malformed, or wrong-length key file disables every
  secret write and every resolution at process start — the process refuses the operation, never
  falls back to plaintext storage or no-op encryption.

### Key versioning and rotation

- `secret_keys` is a small durable table of non-secret key metadata: `key_version`, creation
  time, active flag. The key *material* stays outside the DB; only its identity is recorded.
- Rotation adds a new key version, marks it active, and writes new secrets under it.
  **Re-encryption of existing secrets is explicit and operator-triggered** (a maintenance
  command, not an automatic background sweep), and is idempotent per row.
- Old key material may be retired only after no row references its version; the command refuses
  while references remain.

### Lifecycle

Operations, all owner-scoped and authorization-checked:
`create`, `replace` (new value, bump `rotation_count`), `inspect` (metadata only), `disable`,
`re-enable`, `revoke` (terminal; row retained as evidence, value destroyed by dropping
ciphertext), `delete` (only when no connection references it).

A secret is referenced by at most one active account connection; the reference is checked in the
same transaction as disable/revoke. Revoking a secret fails closed any connection that resolves
through it at the next check — matching Stage D's "revocation at the next safe pre-dispatch
check" rule.

### What is never stored or exposed

- Never persisted: plaintext values, plaintext-derived material, or anything reversible in
  metadata. `repr` of every domain value excludes ciphertext.
- Never returned by any API: secret values, ciphertext, or key material. Responses carry only
  name, provider hint, status, scopes, key version, rotation count, and timestamps.
- Never logged: values, ciphertext, key file contents. Errors are static, safe sentences.
- Never placed in: package manifests, memory items, model prompts, Run events, audit events,
  or browser storage. The dashboard shows a write-only entry form; **no read-back path exists**.

### No broad secret API

Packages have no secret API at all — no enumeration, no read. The SDK gains nothing; tools and
connectors receive resolved credentials only inside NervOS's own process, at the last moment,
through the H2 broker. The API surface is intentionally narrow: create/replace/inspect/disable/
revoke over the owner's own secrets.

## Consequences

- Losing the key file makes all stored secrets permanently undecryptable. Recovery is documented
  (re-enter values); NervOS does not pretend otherwise.
- The API can answer "does this secret exist and is it usable?" but can never show a value —
  the classic trade, accepted deliberately.
- SQLite remains the store; a database file leak exposes ciphertext only, with keys elsewhere
  on disk. A same-user filesystem attacker remains the documented residual risk.
