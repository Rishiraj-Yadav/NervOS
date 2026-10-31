# Linux Bridge Verification Runbook

**Status: every result is [unverified] until the owner runs this and pastes the output
back.** Nothing below has been executed by the authoring agent (Windows machine; no
bubblewrap).

## Prerequisites

- Linux with `bwrap` installed (`bubblewrap` package)
- The repo checkout with `uv` available
- Optional: a throwaway Python venv with `openai`, `anthropic`, `google-genai`
  (`uv venv /tmp/sdk-venv && uv pip install --python /tmp/sdk-venv/bin/python openai anthropic google-genai`)
  and a throwaway Node directory with the Node SDKs for the Node smoke cases

## Steps

```bash
cd <repo>
chmod +x scripts/verify_linux_bridge.sh
./scripts/verify_linux_bridge.sh
```

The script runs, in order:

1. `packages/nervos-core/tests/integration/test_linux_bridge_verification.py`
   - D1 [unverified] loopback bind+connect inside `bwrap --unshare-all`
     (expected: `LOOPBACK_OK`)
   - D2 [unverified] outbound connection blocked (expected: `OUTBOUND_BLOCKED`)
   - D3 [unverified] real `LinuxBubblewrapContainment.prepare` binds the bundle,
     the exec bit works, and the bundle is read-only
     (expected: `EXEC_BIT_OK`, `BUNDLE_READONLY_OK`)
   - D4 [unverified] a write into the bundle fails (expected: `WRITE_BLOCKED`)
2. The real-host launch suite (A1) — the host subprocess runs a real child through
   the shim.
3. The real-socket suites (vendor routes, tool choice + multi-turn, streaming,
   first bridge e2e).
4. The Python SDK smoke (`bridge_sdk_smoke.py`) — expected: 14/14 PASS.
5. Optional Node smoke (`--node-dir ...`) — expected: 15/15 PASS.

## What "verified" means here

Paste the complete script output back into the repository discussion. On success:

- ADR 0041's `[unverified]` loopback claim flips to verified (M2 gate passed).
- ADR 0043's exec-bit item is verified on Linux.
- The implementation-status "Verified with real SDKs" table gains a Linux row.

Until then every claim in Phase D stays `[unverified]`.
