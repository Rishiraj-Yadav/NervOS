# ADR 0029 — Exact Artifact Distribution, Local Stage-G Installation Handoff, and Offline Lifecycle

Status: Accepted for I0 architecture freeze / pending external acceptance

## Context

G already provides the sole package lifecycle. Remote discovery must preserve its exact
authorization, immutable execution and offline properties. See the [master plan](../stage-i/README.md).

## Decision

Distribute immutable objects addressed by archive SHA-256, never raw package paths. ETag is
not SHA-256. Preserve/tighten G archive limits and enforce bounded resource use. Versioned
release descriptors bind exact ID/version, content/archive digests, signer, length and
observed availability/freshness. Latest is presentation, never install or execution identity.
Downloads stay on the configured origin under ADR 0027, reject arbitrary URLs/redirects,
stream raw bytes privately, and validate exact size/transport hash.

Local G verification independently compares ID/version/content/archive/signer with descriptor.
Show locally verified evidence and pre-H warning, then require exact signer/content/archive
authorization. Invoke **the same PackageApplicationService.install**, with its snapshot,
reverification, registry, offline environment, local health check and ACTIVE lifecycle.
No second installer or resolver. Hosted metadata is not activation authority.

I4 defines a bounded owner-bound retained install request: origin, exact identity/digests/signer,
size, private artifact, expiry/status. Approved bytes are retained and reverified by the
installer; no second latest resolution. Existing inspection cleans its own snapshot, so it is
not sufficient retention. Test ownership, expiry, substitution, failure cleanup, retries and
crash reconciliation. Fresh availability checks cannot be replaced by stale cache; observations
cannot eliminate the cross-system status race, which must be disclosed and recorded.

Install yields node-global software, not instance/model/tool binding/grant/trigger/memory.
Users explicitly configure instances after ACTIVE. Updates install exact versions side-by-side;
rebind is explicit G behavior. Accepted queued/running/retryable/historical Runs remain pinned.
Rollback reuses previously installed compatible binding without undoing side effects/history.
Uninstall reuses G obligation-aware removal.

Yanked releases leave ordinary selection, retain history and require strong exact-access warning/
acknowledgement. Revoked releases block hosted distribution and retain history. Advisories/key
status never automatically delete software, disable instances, cancel Runs, revoke D grants or
change trust pre-H. H owns future local enforcement.

Worker/Scheduler/SDK/package-host never depend on Marketplace. Outage blocks new remote reads/
downloads/publication, not installed runs, local tools/history/memory, manual archives or local
rollback. Future I4 may need small separate local request/provenance storage; no I0 migration,
no reserved revision and no changes to Run/Job/Attempt executable truth.

## Consequences and alternatives

Explicit verification/approval costs a review step but preserves G. Metadata-only install,
latest-at-approval resolution, remote push, automatic rebind and a parallel installer are
rejected. Portable external MCP requirements remain a separate future G architecture request,
not hidden distribution scope.

## Verification and governance

I4 proves exact handoff and unchanged manual lifecycle; I5 proves v1/v2 explicit rebind,
historical pinning, warnings and execution with Marketplace stopped. No runtime implementation
in I0. Pending external acceptance; G lifecycle changes require a Stage-G architecture request.
