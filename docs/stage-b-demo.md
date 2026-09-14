# Stage B working demo

Stage B is a bounded, one-shot trusted Chat demonstration. It is not a conversation, memory, tool, queue, worker, streaming, retry, or fallback system.

## Deterministic acceptance demo

The repository E2E journey starts NervOS against a fresh temporary database and two offline provider doubles. It sets up and logs in, creates `nervos.chat@1` with Anthropic, executes and reloads a persisted Run, switches the same instance to OpenAI with another opaque test model, executes again, and reloads both immutable provider/model snapshots. The supervisor removes both provider credential variables and blocks external browser requests.

Run with:

```text
uv run python scripts/check.py e2e
```

No cloud credential, network request, quota, or cost is involved.

## Real-cloud product demo

Run this only after explicitly authorizing provider network use, credential use, and possible cost.

1. Put `ANTHROPIC_API_KEY` and/or `OPENAI_API_KEY` in the API process environment. Do not put credentials in repository files or command arguments.
2. Start NervOS normally and open the dashboard.
3. Complete setup or log in.
4. Create an explicit `nervos.chat@1` Agent Instance.
5. Choose Anthropic or OpenAI and enter a currently supported provider model identifier. NervOS treats it as opaque configuration and does not discover models.
6. Submit one harmless bounded prompt.
7. Verify a terminal persisted Run is displayed, then reload and verify it remains in Run history.
8. If the other provider is configured and separately authorized, update the same instance's provider and model, run once, reload, and verify the older snapshot is unchanged.
9. Stop NervOS and remove credentials from the process environment.

`store=False` disables OpenAI Responses storage as supported by that API; it is not a claim of zero provider-side retention under every provider, security, or legal policy.

## Live-proof status

```text
ANTHROPIC LIVE PROOF — NOT EXECUTED
OPENAI LIVE PROOF — NOT EXECUTED
```

Lack of live credentials is not a software-acceptance failure. Never change either status to executed unless that real cloud proof was explicitly authorized and actually completed.
