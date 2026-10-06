# Package tools, execution context, and memory

This guide documents the approved runtime integration extension in ADR 0031. It is a bounded MVP extension to the existing Run → Job → Worker architecture. Stage H is delivered; see `docs/stage-h/README.md` for the qualified platform and evidence.

## What an owner configures

1. Open **Connections** and add an operator-approved MCP server key or HTTPS endpoint. Discovery exposes the server's tool catalog. A connected server does not authorize an agent.
2. Open an installed package agent and expand **Tools & permissions**. A package can only bind aliases declared by its signed manifest. Choose a tool whose upstream name and input-schema digest match. Grant access explicitly per agent.
3. Schema drift requires review. Revoking a grant blocks the next invocation; it cannot recall an operation already dispatched to an external server. The existing Stage-D mediator still performs every call and writes the durable invocation audit.

Portable package manifests can opt into the additive extension:

```yaml
x-nervos-runtime-integration:
  version: 1
  context: structured-v1
  mcp_tools:
    - alias: research.search
      upstream_name: search
      input_schema_sha256: <64 lowercase hex characters>
      required: true
```

The schema digest is SHA-256 of the tool input-schema JSON serialized as UTF-8 with sorted keys, no insignificant whitespace, and non-ASCII characters preserved. The manifest declares the upstream identity and contract, never a local connection ID. Each owner binds the alias locally. An optional declaration may be unavailable without preventing the agent from running; a required declaration without a binding fails closed.

Packages without the extension retain the legacy rendered-input contract. `structured-v1` packages receive separate current input, selected memory and bounded conversation history through the SDK. They require a package host that advertises `runtime-integration-v1`; an already installed immutable environment is not silently rewritten. Prepare runtime wheels before installing/building a new package environment:

```powershell
uv run python scripts/prepare_runtime_artifacts.py
```

Then install or upgrade the package so its isolated environment uses the prepared host and SDK. The host still proxies every model request through the Worker and configured NervOS provider. Chat roles are preserved. SDK tool-role messages are rejected because the public request contract does not yet carry qualified tool-call identity.

## Context and memory

At each accepted run, NervOS freezes current input, selected memory versions and context. Conversational retries reuse the existing snapshot. Independent runs use selected memory without inventing a conversation or replaying earlier independent-run history. The **Context & memory used** panel shows the retained snapshot for an owned run.

Packages may return up to six typed memory proposals, each at most 2,000 UTF-8 bytes, with a total batch limit of 8,000 bytes. The Worker validates and stores proposals only in the fenced successful Run completion transaction. Packages cannot write the database directly or grant tools through memory.

Open **Memory policy** on the agent page:

- **Manual only** is the default; existing owner-managed memory remains available.
- **Review agent suggestions** stores candidates for an owner to approve or dismiss.
- **Automatically save private agent facts** allows eligible AGENT-scope facts for this instance to be appended automatically. USER-scope facts always require review.

The extractor is separately opt-in. It creates an additional ordinary bounded Run/Job using the configured provider and can incur model cost. It never recursively extracts from its own run. Extraction and memory failures do not change the primary run's result. Deleted conversations and superseded retry results are ineligible for extraction or new facts. Automatic retention does not silently restore a matching fact the owner edited or deleted; a new explicit approval can restore it. Existing immutable snapshots may still contain old memory text after deletion.

Newly stored facts are eligible for later snapshots subject to existing scope, retrieval and context limits. Memory is untrusted data and never grants a permission. Fact extraction can be inaccurate; inspect provenance and approve carefully.

## Runtime health and boundaries

**Runtime health** is an observed aggregate view of Worker availability and the signed-in owner's queued/running/retrying job counts. It does not expose worker IDs, leases, other users' counts, or queue position. It cannot prove provider credentials or external service health.

This MVP adds no new secret, account or approval model of its own; those are Stage H (ADR 0037/0038) and are documented there. Package subprocess isolation is likewise not an ADR 0031 concern: package execution now runs through the Stage-H pre-exec bubblewrap launcher on Linux and is refused elsewhere (see `docs/runtime.md`). No universal LangChain/LangGraph compatibility or durable framework checkpoint support is claimed.
