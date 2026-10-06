# NervOS Agent Packages

## Current status

G0–G4 define and implement the local `.nervos` package format, exact versioned registry, package
AgentInstances, and explicit lifecycle surfaces. **G5 is complete and externally accepted, pending
merge; Stage G is complete and externally accepted, pending merge.** Bootstrap prepares real local
SDK/package-host wheels, package ToolPort calls use the shared Stage-D mediator, and real-host plus browser acceptance proves
the install-to-Run path. Stage H adds per-action approval, encrypted secret management, account
credential brokering, and persistent publisher trust. Package execution is currently refused on
every platform until a combined filesystem/network isolation launcher is qualified.
The frozen contract is in `docs/stage-g/README.md` and accepted ADRs 0024–0026; Stage H's is in
`docs/stage-h/README.md` and ADRs 0032–0036.

Stage G remains a dependency-isolation and process-boundary design on its own. Stage H does not treat
Windows Job Objects or POSIX rlimits as sufficient package isolation: they are resource primitives,
not filesystem/network isolation. Containment is resolved *before* the spawn, and a platform
that cannot establish it — macOS — makes the Worker **refuse to execute** the package with an
explicit error rather than falling back to running it unsandboxed. Package code still never
receives credentials: the broker resolves them server-side at dispatch time.

## Publisher trust

Stage H (ADR 0036) adds a persistent local publisher-trust store. A signature proves integrity, not
trust: installing a package from a publisher requires an explicit local trust decision, and a
**revoked** publisher blocks install, rebind, and any new execution. Already-installed packages,
already-queued Runs, and historical Run records keep their defined behavior — revocation is a
forward-looking gate, not a retroactive rewrite.

## Package artifact and identity

A `.nervos` artifact is a ZIP containing a strict `manifest.yaml`, one prebuilt pure-Python
`py3-none-any` agent wheel, a bounded JSON configuration schema, and integrity/signature metadata.
Optional assets and a self-contained dependency wheelhouse follow the frozen archive contract. A
package cannot include database migrations or executable install/configuration hooks.

The exact identity is `package_id@package_version`. Package IDs use the NervOS agent-key grammar;
the reserved `nervos.*` namespace belongs to built-ins. Versions use strict SemVer. Package ID is
the AgentDefinition key and package version is its definition version. Resolution is exact: there is
no `latest`, implicit fallback, or automatic upgrade.

The builder produces canonical archive metadata. The verifier checks archive paths and bounds,
wheelhouse closure, content digests, and Ed25519 signatures without importing or executing package
code. A valid signature proves integrity and authenticity relative to the key included in that
artifact; it does **not** establish that the publisher is trusted. NervOS shows the signer
fingerprint and content digest, and an operator explicitly authorizes installation. There is no
persistent trust store.

### Admission hardening in the current unaccepted worktree

The user-authorized amendment in ADR 0031 adds Requires-Dist admission budgets:
64 KiB per decoded logical value, 4 MiB of values across agent/dependency wheels,
and 100,000 occurrences across the package. Duplicates and inactive markers count.
The shared builder/verifier enforces these maxima before dependency parsing;
production configuration cannot raise them. This intentionally rejects some
previously accepted packages on fresh inspection. Stored signed bytes and registry
records are unchanged; reinspection has no grandfathering bypass.

Unused wheel metadata and descriptions are streamed and discarded. Large PEP 440
Version values use packaging's public grammar and chunked text normalization,
preserving acceptance without a new Version limit. Ordinary version values keep
the existing normalization path. The resource qualification and I2/I3 acceptance
gates remain in progress; this subsection does not declare a delivered milestone.

## Install and run

Inspection verifies an artifact and returns bounded safe metadata. It does not execute package code,
install anything, create an AgentInstance, or persist trust. Installation verifies the artifact
again, stages an immutable snapshot, checks the authorization against that exact artifact, builds a
content-addressed Python 3.12 environment from local wheels only, and health-checks the package
entrypoint before activation.

The runtime's own `nervos-sdk` and `nervos-package-host` wheels are prepared locally by
`scripts/prepare_runtime_artifacts.py` during `scripts/bootstrap.py`. They are stored under the
configured package store's `runtime/` directory. Package environment creation validates these real
wheel files and fails closed if either artifact is missing or invalid; installation does not
download or fabricate runtime artifacts.

Installing a package version does not create an AgentInstance. An operator creates an ordinary
owner-scoped AgentInstance from an exact ACTIVE package version and chooses its provider, model, and
configuration. Configuration is schema-validated by the server, canonicalized, revisioned, and
snapshotted into every accepted Run. A Run and its retries stay pinned to that package version,
environment, entrypoint, and configuration even after later config changes or rebinds.

Package execution follows the normal Run → Job → Attempt → Worker path. The Worker launches the
package in its isolated environment through `nervos-package-host`. Model requests go through the
existing provider abstraction. For each package ToolPort request, Worker loads the verified manifest
for the exact installed package version pinned by the Run, requires the name in its required or
optional tool declarations, resolves it to a portable built-in Stage-D descriptor, then invokes the
same mediator used by ToolLoop. Stage-D grant evaluation, timeout, executor dispatch, normalized
results, and `tool_invocations` audit remain the authority. A declaration does not create a grant;
undeclared or ungranted requests are denied safely. Conversation context comes from the Stage-F
snapshot. Package code receives no provider credentials, database access, Worker claim authority, or
direct memory repository access. Package declarations do not grant tools, create triggers, or write
memory automatically.

## Versions and lifecycle

Multiple exact versions can remain installed side by side. Installing another version does not
change existing AgentInstances or historical Runs. Rebind and rollback are explicit operations that
switch an AgentInstance to another compatible ACTIVE version; config is carried forward only when
the target schema accepts it, otherwise the operator supplies new config. Immutable config paths
remain protected. Rebind affects future Runs and never rewrites historical Run snapshots.

Removal is obligation-aware. A package version with bound AgentInstances is blocked until those
instances are explicitly rebound or otherwise unbound. A version with nonterminal Runs enters
`pending_removal`, rejects new bindings and admissions through that binding, and is finalized after
those Runs drain. Removed versions retain durable tombstones for historical Run references. An
environment remains while any durable Run references it, including terminal Runs.

## Dashboard and CLI

The dashboard's **Packages** area supports local artifact inspection, signer/content review,
explicit installation, package listing/details, AgentInstance creation/configuration, rebind,
rollback, and removal planning. It uses the authenticated API for lifecycle operations.

The CLI also uses the API; it does not access the database or package store directly. Commands
include:

```text
nervos auth login|logout|status
nervos package inspect <file>
nervos package install <file>
nervos package install <file> --yes --approve-signer <full-fingerprint> --approve-content-digest <full-digest>
nervos package list|show|uninstall ...
nervos agent create|config|rebind|rollback ...
```

Interactive install displays the signer fingerprint and content digest before asking for approval.
Noninteractive install requires both complete values; `--yes` alone is rejected. The CLI's local
session is bounded and its cookie is not printed. Authentication remains a local pre-Stage-H
operator boundary, not a new API-token or publisher-trust system.

## Deterministic local demo

The repository's signed browser-acceptance fixture is also a credential-free local demo. It uses a
test signing key and returns a fixed result without requesting a model completion; do not use the
fixture or its key for production packages. From the repository root, prepare dependencies and the
real SDK/package-host runtime wheels, then build the artifact:

```powershell
python scripts/bootstrap.py
python tests/e2e_support/build_stage_g_package.py "$env:TEMP\browser-runtime-demo.nervos"
```

Start each service in its own terminal:

```powershell
uv run python scripts/dev.py api
uv run python scripts/dev.py worker
uv run python scripts/dev.py web
```

Open `http://localhost:5173`, complete first-run setup if prompted, and open **Packages → Install
Package**. Select the file created at `$env:TEMP\browser-runtime-demo.nervos`. Review the verified
signer and content digest and choose **Authorize & Install**. After the package is active, create an
AgentInstance using `com.acme.browserdemo@1.0.0`, choose a provider and an unused model label, and
submit a Run. Those fields are required, but the fixture does not call ModelPort and needs no live
provider credential; it returns `stage-g-browser-runtime-ok` without contacting OpenAI, Anthropic,
or a package registry. Bootstrap must complete first so package installation can consume the
prepared local runtime wheels.

## Compatibility and security limits

Package dependencies are installed offline from the artifact's exact wheelhouse alongside the exact
NervOS-controlled SDK and package-host distributions. No package network resolver or runtime `uv`
requirement is used. This protects the shared NervOS Python environment and gives installed versions
repeatable dependency environments.

The package-host subprocess and child environment allowlist are defense-in-depth boundaries, not
containment against malicious code. Stage G does not prevent a package from accessing the host
filesystem or network, starting child processes, exhausting resources, or exploiting the operating
system. Do not install artifacts unless their code and signer are acceptable to the operator.

Marketplace discovery and publishing, persistent publisher trust, automatic upgrades, hostile-code
sandboxing, and multi-agent orchestration are outside Stage G.
