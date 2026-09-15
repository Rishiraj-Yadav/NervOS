# NervOS configuration

NervOS uses typed settings backed only by process environment variables. It does not automatically load `.env`, `.env.local`, or any other environment file. `.env.example` is documentation containing non-secret values only.

## Shared variables

| Variable | Default | Rules |
|---|---|---|
| `NERVOS_ENVIRONMENT` | `development` | One of `development`, `test`, or `production`. |
| `NERVOS_DATABASE_PATH` | `~/.nervos/nervos.db` | Must be non-blank and identify a file rather than an existing directory. User-home syntax is expanded and the path is resolved without creating it. |
| `NERVOS_LOG_LEVEL` | `INFO` | One of `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |

A relative database path is resolved from the process working directory. Repository commands run from the repository root; deployments should normally provide an absolute path. Settings validation performs no filesystem mutation. The API migration command creates the database parent directory immediately before connecting.

## API-only variables

| Variable | Default | Rules |
|---|---|---|
| `NERVOS_APP_ORIGIN` | `http://localhost:5173` | One exact HTTP(S) origin with a host. Wildcards, credentials, paths, trailing slashes, queries, fragments, and invalid ports are rejected. Production requires HTTPS. |
| `NERVOS_MAX_PENDING_JOBS` | `1000` | Global hard cap for accepted-but-unfinished Jobs. Integer from 1 through 100000. |

The API/control plane holds no provider credential and constructs no provider SDK client. It validates that a provider identifier is known and durably accepts Runs for known providers; actual provider capability belongs to Workers.

## Worker-only variables

| Variable | Default | Rules |
|---|---|---|
| `NERVOS_WORKER_CONCURRENCY` | `1` | Local execution slots for this Worker process. Integer from 1 through 16. |
| `NERVOS_MAX_ACTIVE_JOBS` | `4` | Node-wide active Job cap enforced during claim. Integer from 1 through 16. |
| `ANTHROPIC_API_KEY` | unavailable | Optional process-only credential for Anthropic. Empty or whitespace-only values mean unavailable. Read only by the Worker. |
| `OPENAI_API_KEY` | unavailable | Optional process-only credential for OpenAI Responses. Empty or whitespace-only values mean unavailable. Read only by the Worker. |
| `NERVOS_WORKER_READY_FILE` | unset | Test-only readiness marker path used by the deterministic E2E supervisor. Production should not set it. |

A Worker claims only Jobs whose `model_provider` is in its configured provider set. A Worker with no provider credentials starts successfully, claims nothing, and fails nothing.

Real environment files, credentials, API keys, and session secrets must remain local and untracked. A3 authentication adds no operator-configurable secrets. Cookie security is derived from the existing validated environment and origin settings.
