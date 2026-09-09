# ADR 0006: Local authentication security boundary

- Status: Accepted
- Date: 2026-09-09

## Context

Stage A needs first-run initialization and local dashboard authentication without introducing external identity providers, browser-held bearer tokens, or a singleton administrator schema. The dashboard also needs a public way to distinguish an empty installation from an initialized one.

## Decision

NervOS uses the general `users` table. Initial setup is available only while no user exists; the first user receives `role="admin"` and `is_active=true` server-side. SQLite `BEGIN IMMEDIATE` makes the in-transaction existence check authoritative. No fixed user ID or singleton-administrator invariant is used.

`GET /api/v1/setup/status` exposes only whether any user exists. Setup and login accept JSON credentials. Usernames are NFKC-normalized, trimmed, and casefolded before the 3–32 ASCII policy is applied. Passwords are preserved exactly and limited to 12–128 Unicode code points and 512 UTF-8 bytes.

Passwords use Argon2id with the RFC 9106 low-memory profile. Expensive password work is bounded by a process-local semaphore; saturated requests receive the safe `429 too_many_attempts` response. This is resource bounding for one API process, not a replacement for reverse-proxy or network rate limiting on externally exposed deployments.

Authentication uses random opaque cookies and server-side sessions. Tokens have 256 bits of source entropy; only their 32-byte SHA-256 digest is stored. Sessions expire absolutely after seven days, do not slide, and logout revokes only the presented session.

Every unsafe `/api/v1` request requires the exact configured Origin. Credential requests are JSON-only and limited before downstream parsing. API responses use `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`; authentication and credential responses are non-cacheable. No permissive CORS is enabled.

## Consequences

Without a bootstrap secret, the first reachable client can claim an empty installation. Operators must initialize NervOS while it is reachable only from loopback or another trusted interface. The implementation remains synchronous so FastAPI executes SQLAlchemy and Argon2 work in worker threads. Authentication sessions remain separate from future agent conversation sessions.
