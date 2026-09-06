# ADR 0003 — Server-Side Opaque Authentication Sessions

Status: Accepted

## Context

NervOS needs secure local dashboard authentication. Browser-stored bearer tokens complicate revocation and increase accidental exposure.

## Decision

Use cryptographically random opaque server-side sessions. The browser gets an HttpOnly cookie; the DB stores only a cryptographic hash of the session token. Passwords use Argon2id. Expiration and revocation are server-side.

## Consequences

Logout/revocation are straightforward and no authentication token is stored in localStorage. This ADR concerns dashboard login sessions, not future agent conversation sessions.
