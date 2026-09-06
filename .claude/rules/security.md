# NervOS Security Rules

Security boundaries are part of the architecture, not optional polish.

## Secrets

Never:
- hard-code API keys
- commit API keys
- print secrets in logs
- place secrets in agent memory
- put secrets into agent package manifests
- expose secret values through API responses

Secrets must eventually be handled through the NervOS Secret Manager.

## Sensitive files

Do not read or modify:
- `.env`
- `.env.*`
- private keys
- credential files
- `secrets/`

unless the user explicitly requests it.

Use `.env.example` with fake values for documentation.

## Authentication

For the local NervOS dashboard use server-side opaque sessions.

Do not store authentication tokens in localStorage.

Session cookies must be:
- HttpOnly
- SameSite configured appropriately
- Secure when HTTPS/production is enabled

Password hashes must use Argon2id.

Raw session tokens must not be stored in the database.
Store a cryptographic hash of the session token.

Authentication error messages must not expose credentials or hashes.

## Initial setup

Admin account creation is allowed only while no account exists.

After initial setup, bootstrap/setup endpoints must not allow another
unauthenticated admin to be created.

## Authorization

Every protected backend route must establish the authenticated user.

Never trust a user_id supplied by the browser as proof of identity.

Ownership must be checked server-side.

## API

Validate all external inputs through Pydantic schemas.

Never interpolate untrusted input into SQL or shell commands.

Do not expose Python exceptions or stack traces through production API responses.

## CORS

Never use `*` together with credentialed browser authentication.

Prefer same-origin API access.

For development, use the Vite proxy where practical.

## Logging

Never log:
- passwords
- session tokens
- API keys
- authorization headers
- OAuth refresh tokens

## Future agent security

Marketplace agents must be considered untrusted.

Do not design APIs that require arbitrary agent code to receive raw credentials.

Future tool calls must pass through:
Agent → Permission Engine → MCP Gateway → Tool.

Do not allow agents to bypass the permission layer by directly accessing
host resources.