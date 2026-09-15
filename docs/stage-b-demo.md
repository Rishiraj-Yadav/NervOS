# Trusted Chat demos

The original Stage B demo proved a bounded, one-shot trusted Chat behavior. C2 keeps that trusted behavior but changes execution from an awaited API-process call to durable asynchronous Worker execution.

It is still not a conversation, memory, tool, streaming, retry, fallback, scheduler, or cancellation system.

## Deterministic acceptance demo

The repository E2E journey starts NervOS against a fresh temporary database and offline provider doubles. It sets up and logs in, creates `nervos.chat@1`, submits a Run, observes the queued state, waits for the Worker to execute it, reloads the persisted terminal result, and proves the API/Worker/Web journey without cloud credentials.

Run with:

```text
uv run python scripts/check.py e2e
```

No cloud credential, network request, quota, or cost is involved. The supervisor removes both provider credential variables from the API environment and gives the Worker deterministic completions through the E2E-only composition.

## Real-cloud product demo

Run this only after explicitly authorizing provider network use, credential use, and possible cost.

1. Put `ANTHROPIC_API_KEY` and/or `OPENAI_API_KEY` in the **Worker** process environment. Do not put credentials in repository files or command arguments.
2. Start NervOS in this order: API, Worker, then Web. The API migrates the database; the Worker validates the schema and claims Jobs only for configured providers.
3. Open the dashboard, complete setup or log in, and create an explicit `nervos.chat@1` Agent Instance.
4. Choose Anthropic or OpenAI and enter a currently supported provider model identifier. NervOS treats it as opaque configuration and does not discover models.
5. Submit one harmless bounded prompt. The API should return/record a queued Run first; the Worker should later move it to a terminal Run.
6. Verify a terminal persisted Run is displayed, then reload and verify it remains in Run history.
7. If the other provider is configured in the Worker and separately authorized, update the same instance's provider and model, submit once, reload, and verify the older snapshot is unchanged.
8. Stop NervOS and remove credentials from the process environment.

`store=False` disables OpenAI Responses storage as supported by that API; it is not a claim of zero provider-side retention under every provider, security, or legal policy.

## Manual live proof helper

`scripts/manual_anthropic_b2_proof.py` is retained as an operator-only live proof helper, but it now exercises the C2 durable path: submit, claim, execute through `RunExecutor`, and read the terminal Run. It is excluded from pytest, CI, E2E, and clean-check. Running it requires explicit live-provider authorization and a disposable database path.
