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
| `NERVOS_MAX_PENDING_JOBS_PER_AGENT` | `1000` | Hard cap for accepted-but-unfinished Jobs belonging to one Agent Instance. Integer from 1 through 100000. |
| `NERVOS_MAX_PENDING_JOBS_PER_PROVIDER` | `1000` | Hard cap for accepted-but-unfinished Jobs belonging to one model provider. Integer from 1 through 100000. |

The three pending caps are **admission (backlog) limits, not execution-concurrency limits**: they bound how much work NervOS accepts, not how much it runs. All three are counted, and the Run and Job are written, inside the same admission transaction, so a rejection never leaves a partially created Run. Each per-dimension cap defaults to the global bound, so neither binds until an operator lowers it; lowering one reserves queue headroom so that a single Agent Instance or provider backlog cannot refuse every other submitter.

The API/control plane holds no provider credential and constructs no provider SDK client. It validates that a provider identifier is known and durably accepts Runs for known providers; actual provider capability belongs to Workers.

## Stage H security variables

| Variable | Default | Rules |
|---|---|---|
| `NERVOS_SECRETS_KEY_FILE` | `~/.nervos/secrets.key` | Path to the Secret Manager key. The key lives **outside** the database; this is the only place key material is read from. |
| `NERVOS_SECRETS_KEY_VERSION` | `1` | Key version recorded alongside each stored secret so rotation is auditable. |

Generate a base64 key before storing any secret, and treat it as a backup you must keep: **losing the key
makes every stored secret permanently undecryptable**, and recovery means re-entering values after
re-keying. The file must be readable by the owning user only.

```bash
python -c "import base64,secrets,pathlib,os;p=pathlib.Path.home()/'.nervos'/'secrets.key';p.parent.mkdir(parents=True,exist_ok=True);p.write_text(base64.b64encode(secrets.token_bytes(32)).decode());os.chmod(p,0o600)"
```

`.env.example` documents both variables with placeholder values and the generation command. Never
place real key material in an environment file, a log, or any document.

## Execution-concurrency policy

Global, per-Agent-Instance, and per-provider **execution** concurrency is not operator-configurable. It is one code-level policy in `nervos-core`, defaulting to 4 concurrent Jobs globally, 4 per Agent Instance, and 4 per provider. A claim compares a database-wide count of live leased Jobs against these limits, so two Workers holding different values would let the fleet run above the intended bound; shipping them as shared constants is what makes the limits authoritative. Changing them means changing policy code. See ADR 0013.

## Worker-only variables

| Variable | Default | Rules |
|---|---|---|
| `NERVOS_WORKER_CONCURRENCY` | `1` | Local execution slots for this Worker process. Integer from 1 through 16. This bounds one process's parallelism and is **not** fleet concurrency. |
| `NERVOS_MAX_ACTIVE_JOBS` | `4` | Tightening-only ceiling on live Jobs this Worker holds. Integer from 1 through 16. It may under-claim relative to the execution-concurrency policy, and it can never raise it. |
| `ANTHROPIC_API_KEY` | unavailable | Optional process-only credential for Anthropic. Empty or whitespace-only values mean unavailable. Read only by the Worker. |
| `OPENAI_API_KEY` | unavailable | Optional process-only credential for OpenAI Responses. Empty or whitespace-only values mean unavailable. Read only by the Worker. |
| `GEMINI_API_KEY` | unavailable | Optional process-only credential for the Google Gemini Developer API (AI Studio), not Vertex AI. Empty or whitespace-only values mean unavailable. Read only by the Worker. |
| `NERVOS_WORKER_READY_FILE` | unset | Test-only readiness marker path used by the deterministic E2E supervisor. Production should not set it. |
| `NERVOS_ACCOUNT_OAUTH_PROVIDERS` | empty | JSON array of operator-declared OAuth providers. Each item supplies only `id`, authorization/token HTTPS endpoints, public client id, callback URL, allowed scopes, and optional revocation endpoint/client-secret environment-variable name. |
| `NERVOS_ACCOUNT_TOOL_BINDINGS` | empty | JSON array of explicit reviewed tool/fingerprint → account/endpoint/scope bindings. MCP definitions also require `mcp_connection_id`; native Gmail list/get omit it and require the fixed Gmail origin and read-only scope (ADR 0040). Empty supplies no account credential. |

A Worker claims only Jobs whose `model_provider` is in its configured provider set. A Worker with no provider credentials starts successfully, claims nothing, and fails nothing.

There is deliberately **no setting to weaken package isolation**. Package execution requires a
Linux host with the `bwrap` (bubblewrap) binary installed; the Worker prepares that launch before
any package code starts and otherwise refuses the Run with `sandbox_unavailable`. Nothing in the
configuration can downgrade that, and there is no development bypass. See
[Package execution isolation](runtime.md#package-execution-isolation).

OAuth providers and account-tool bindings are operator configuration, not package configuration. A
package cannot discover an account token or choose a new destination. The owner completes the PKCE
browser flow at `/security`; NervOS stores tokens only in the encrypted Secret Manager. Do not put
real values or JSON containing client secrets into an environment file committed to the repository.

### Use Gemini Developer API

Create an API key in [Google AI Studio](https://aistudio.google.com/apikey). Set it only in the Worker process environment, then restart the Worker. For example, in PowerShell:

```powershell
$env:GEMINI_API_KEY="<your-api-key>"
uv run python scripts/dev.py worker
```

In NervOS, create a Chat or package Agent Instance, select **Gemini**, and enter an exact Gemini Developer API model ID in **Model**. The model name is stored as entered and is checked when a Run executes. The API accepts a Gemini Run even if no Gemini-capable Worker is currently running; the Run waits in the durable queue until one is available. This integration uses the Gemini Developer API with an AI Studio API key. Vertex AI credentials and project/location routing are outside this provider.

An optional, operator-invoked smoke check is `uv run python scripts/manual_gemini_proof.py --model <exact-model-id>`. It makes one bounded text request and is never part of automated tests. With no `GEMINI_API_KEY`, it reports `GEMINI LIVE PROOF — NOT EXECUTED`.

Real environment files, credentials, API keys, and session secrets must remain local and untracked. A3 authentication adds no operator-configurable secrets. Cookie security is derived from the existing validated environment and origin settings.
