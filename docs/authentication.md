# NervOS authentication

Milestone A3 implements local dashboard authentication. It uses Argon2id passwords and opaque server-side sessions; it does not use JWT, browser storage, OAuth, registration, or user-management endpoints.

## First-run setup

`POST /api/v1/setup` accepts JSON `{"username":"...","password":"..."}` only while `users` is empty. The server canonicalizes the username, fixes `role=admin` and `is_active=true`, hashes the password, and atomically commits the user and initial session. Success returns `201` with only `id`, `username`, `role`, and `is_active` and logs the user in. Once any user exists, setup returns `409 setup_complete` permanently because A3 has no user deletion.

SQLite setup executes `BEGIN IMMEDIATE` before checking for an existing user. Password hashing and token generation happen before this short critical section. Concurrent callers therefore produce one successful setup and one stable conflict, never two users.

Without a separate bootstrap secret, the first reachable client can claim an empty installation. Initialize NervOS while it is bound to loopback or otherwise reachable only through a trusted interface.

## Input policy

Usernames are Unicode NFKC-normalized, trimmed, and casefolded, then must contain 3–32 ASCII characters. The first character is alphanumeric; remaining characters may also include `_`, `.`, and `-`.

Passwords are preserved exactly and must contain 12–128 Unicode code points and at most 512 UTF-8 bytes. NervOS does not strip, normalize, truncate, or require composition rules; whitespace remains significant. Password request fields use a non-revealing Pydantic type and sanitized validation errors never include submitted values.

## Password security

Passwords use Argon2id with the RFC 9106 low-memory profile: 64 MiB, three iterations, parallelism four, 16-byte salt, and 32-byte hash. Unknown usernames execute verification against an adapter-owned valid dummy hash and receive the same response as an incorrect password. Successful login can replace a hash whose parameters are obsolete. Expensive Argon2 operations are process-wide concurrency bounded; excess simultaneous work receives `429 too_many_attempts`. A deployment proxy should additionally enforce request-rate limits when the API is exposed beyond loopback.

Passwords and password hashes are never returned or logged.

## Sessions and cookies

A session token is generated from 32 cryptographically random bytes and encoded for cookie transport. Only `SHA-256(token).digest()`—exactly 32 binary bytes—is persisted. The raw token appears only in the `nervos_session` cookie.

Sessions have a seven-day absolute lifetime with no sliding renewal. Multiple sessions are allowed. Logout revokes only the presented session and is idempotent.

Cookie policy:

- name `nervos_session`
- host-only; no `Domain`
- `Path=/`
- `HttpOnly`
- `SameSite=Lax`
- `Max-Age=604800` and matching `Expires`
- `Secure` in production and whenever `NERVOS_APP_ORIGIN` uses HTTPS
- `Secure=false` only for configured HTTP development/test origins

Credential and session responses use `Cache-Control: no-store`.

## API contract

| Endpoint | Behavior |
|---|---|
| `GET /api/v1/setup/status` | Return only `{"setup_complete": boolean}` based on whether any user exists. |
| `POST /api/v1/setup` | Create first admin and session; `201`, or `409` after setup. |
| `POST /api/v1/auth/login` | Create fresh session; `200`, or generic `401`. |
| `POST /api/v1/auth/logout` | Revoke current session when present, clear cookie; always `204`. |
| `GET /api/v1/auth/me` | Return safe current-user data; otherwise generic `401`. |

Every unsafe `/api/v1` request must include an `Origin` header exactly equal to `NERVOS_APP_ORIGIN`. Missing, duplicate, `null`, wildcard, malformed, and mismatched origins return `403 invalid_origin` before body, credential, or database work. Setup and login accept only `application/json` and credential bodies are limited to 4096 received bytes before downstream parsing. A3 does not add CORS; the dashboard uses same-origin API access through the Vite development proxy.

API responses include `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`. See ADR 0006 for the security-boundary decision.

Errors use `{"error":{"code":"...","message":"..."}}`. Invalid usernames/passwords return sanitized `422`; credential failures do not distinguish username existence; persistence contention maps to generic `503`. Responses never expose raw tokens, hashes, SQL, paths, exceptions, or stack traces.
