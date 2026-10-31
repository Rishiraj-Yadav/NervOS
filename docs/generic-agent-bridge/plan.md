# Generic Process Agent Bridge — Design Plan

**Status: PARTIAL** — Core milestones completed; missing items listed below.

**ADR 0042 update (function calling + streaming)**: the shim's
`/v1/chat/completions` now supports OpenAI `tools`, `tool_choice` (auto/none),
assistant `tool_calls`, `role: tool` messages, and `stream: true` replayed as SSE,
with the Worker mapping onto the core `ModelRequest`/`ModelResponse` tool fields and
the initialize frame listing the run's real granted tools. See
`docs/adr/0042-bridge-model-tools-and-streaming.md`.

**What remains**:
- Linux verification run by the owner (`scripts/verify_linux_bridge.sh`,
  `docs/linux-verification-runbook.md`): loopback in bubblewrap stays `[unverified]`
  until then
- Real streaming (incremental generation; today every route replays one complete
  result)
- A per-run model allowlist for the discovery routes (they list the last-used model
  or `nervos-model`)
- Anything else the owner's Linux run surfaces

**ADR 0046 update (process-kind install)**: migration 0025 adds `runtime_kind` to the
installed package versions table and makes the entrypoint columns nullable under a
kind/entrypoint pairing CHECK; the run-executable snapshot carries the stable
`process:<command[0]>` label in the existing `package_entrypoint` column. Process-kind
packages now install, bind, and run through the real Worker adapter with the same
mediation and audit as python-kind (`apps/worker/tests/integration/
test_real_worker_process_package.py`). Existing databases must run
`uv run alembic -c apps/api/alembic.ini upgrade head`.

**ADR 0045 update (tool choice + tool-aware history)**: forced `tool_choice`
(required/named) and ordered multi-turn conversations now flow core → adapters →
wire → Worker → shim on every route; the ADR 0042 refusals are gone. See
`docs/adr/0045-bridge-tool-choice-and-tool-history.md`.

**ADR 0043 update (item 3)**: resolved by "bundle ships its runtime" — a Node package
carries `bin/node` + `node_modules` as assets and launches via
`command: ["bin/node", "app.js"]`.

**Verified with real SDKs**: see the tables in `docs/implementation-status.md`
(Python 14/14; Node 15/15 across openai 7.43.0, @anthropic-ai/sdk 1.3.0,
@google/genai 2.31.0, ai 7.0.153).
