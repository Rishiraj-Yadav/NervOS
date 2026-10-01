# NervOS Stage I — Marketplace Governance Master Plan

## 1. Status and I0 baseline

**I0 IMPLEMENTED AS GOVERNANCE / PENDING EXTERNAL ACCEPTANCE.** The preceding planning
architecture was accepted for this governance freeze with review refinements. These final
documents await external acceptance; that approval is a prerequisite to I1 authorization.
**I1 NOT STARTED. Marketplace runtime functionality NOT IMPLEMENTED. Stage H NOT STARTED.**
ADRs 0027–0030 and this master plan govern future I1–I5. They introduce no runtime behavior.

Reviewed baseline: `main` and `origin/main` at
`1253daa3b06c37a2bbba7e96d5a01f74fbc5ea6b`; Stage-G merge
`48039ab6940ed13b7e460b5985b73beae5bc56e2`; Gemini feature
`bb38fe25793bef5d2d0dfebf7aacaa924fa84c08` merged by the current HEAD.
Local Alembic head and Worker/Scheduler guards: `0013_stage_g3_package_registry`.
Predecessor plan blobs: F `62cfc0a8039e233c82e2a30ff3fd59495e99395d`,
G `4554ab5e93b35f4bbc4daed164ee5712077c1006`. First Stage-I ADR: 0027.

## 2. Verification protocol

Before each milestone, read `../implementation-status.md`, this plan and ADRs 0027–0030;
inspect current source, Git baseline and migration heads; report the current Stage-I plan
blob using `git hash-object docs/stage-i/README.md`; confirm predecessor acceptance and
explicit milestone authorization. Report frozen F/G hashes if touching their seams.
Acceptance requires the milestone's matrix below, focused deterministic tests, full
repository check, E2E and documentation review. Never mark unverified work accepted.
The plan's own blob is recorded externally, never inside this file.

## 3. Purpose

Stage I lets developers publish completed signed `.nervos` artifacts and users discover and
obtain exact releases. Each user's local NervOS independently verifies, explicitly authorizes
and installs those releases through Stage G. Marketplace distributes; local NervOS controls
installation and execution.

## 4. Current A–G implementation truth

The authoritative delivered-state record is `../implementation-status.md`. A–G are complete,
accepted and merged: local authentication/dashboard, provider-neutral agents, durable
Run/Job/Attempt/Worker execution, mediated tools/MCP, triggers/Scheduler, conversations and
scoped memory, and signed package building/verifying/installing/executing/lifecycle management.
`nervos-sdk` and package-host exist. Production provider IDs are `anthropic`, `openai`,
`gemini`; provider credentials stay in the Worker. No Marketplace service/client exists.

## 5. Intentional I-before-H sequence

The operator intentionally selected **G → bounded/pre-H I → H hardening**. Historical stage
letters are unchanged: H is Security isolation; I is Marketplace. This decision does not
transfer H responsibilities into I or claim that H is complete.

## 6. Pre-H Marketplace mode

I1–I5 may deliver hosted publisher/project ownership, public-key association, signed upload,
static verification, immutable publication, bounded catalog/search, safe documentation/assets,
exact distribution, local install handoff, update availability and advisory/status warnings.
They must not claim hostile-code sandboxing, filesystem/network/resource containment,
encrypted secret management, interactive Ask/Approve/Deny policy, persistent local publisher
trust, automatic package trust or automatic local revocation enforcement. A valid signature
and static verification do not establish that a package is safe to execute.

## 7. Authority matrix

| Stage | Authority preserved |
| --- | --- |
| C | Run/Job/Attempt execution, claims, leases, retries, cancellation and terminalization |
| D | Explicit tool grants, live call-time permission, MCP/tool mediation and audit |
| E | Automatic Run creation through the existing acceptance seam |
| F | Conversations, context snapshots and scoped memory |
| G | Package identity/format/signatures, verification, installation, executable pinning and lifecycle |
| H | Sandbox, secret manager, interactive approvals, persistent local trust and local revocation policy |
| I | Hosted publisher/project ownership, catalog, immutable publication and distribution observations |

Marketplace never creates local Runs, Jobs, Attempts, schedules, retries or terminal writes;
calls no model, tool or MCP; writes no local memory. It cannot push local runtime mutations.

## 8. Core mental model

```text
Developer builds/signs locally → Marketplace static verification → immutable publication
Browser → authenticated local API → configured Marketplace → exact artifact
Local verify → explicit authorization → PackageApplicationService → InstalledPackageVersion
Separate explicit AgentInstance configuration → ordinary Run/Job/Attempt → Worker/package-host
```

AgentPackage != AgentInstance; AgentInstance != Run; Session != Run; Memory != Session.
Publisher account identity != package signer identity != local trust decision.

## 9. Hosted/local security boundary

Future `apps/marketplace` is a separate hosted service/security domain with its own database,
object store, authentication, audit and migration lineage. Its existing placeholder stays
inactive during I0; I1 owns activation. Hosted tables never enter `~/.nervos/nervos.db`.
Local SQLite retains packages, instances, execution, triggers, conversations, memory and
operator configuration. Marketplace has no local database or execution-plane access.

## 10. Data ownership and privacy

Requests carry only public discovery/download identifiers and necessary protocol information.
Do not send local user IDs/usernames by default, instance configuration, conversation/context,
memory, Run inputs/results, execution logs, provider keys, MCP secrets, credentials or grants.
No background telemetry by default. Hosted access logs can observe network metadata; later
implementation must document minimization, retention and access. Publication credentials
belong to the hosted domain, never the local dashboard session.

## 11. Production deployment target

Hosted metadata targets a PostgreSQL-class relational database; package artifacts/assets
target private S3-compatible immutable object storage. No cloud vendor is frozen. CI must
prove production-equivalent PostgreSQL constraints/concurrency and conditional object writes.
Do not introduce distributed infrastructure without measured need. Static verification is
bounded infrastructure work, never a C Job/Attempt or Agent Run. I1/I2 select synchronous work
or Marketplace-owned tasks from measured latency/recovery/resource needs; no queue framework,
Celery, Redis or broker is frozen here.

## 12. Package identity and versions

Reuse G exactly: `package_id == agent_key`; `package_version == agent_definition_version`.
Current ID grammar is `[a-z][a-z0-9]*(?:\.[a-z0-9]+)+`, bounded to 128 characters;
`nervos.*` remains reserved. Versions use strict G SemVer (maximum 64 characters), including
prerelease/build identity; build metadata does not change precedence. No normalization,
fallback or substitution. Marketplace UUIDs are storage IDs, never runtime identity.
Hosted API version is independent of manifest, package, SDK and package-host versions.

## 13. Publisher identity

A MarketplaceAccount represents a hosted person/account; ExternalIdentity associates an
issuer and subject. Publisher represents an individual or organization; PublisherMembership
gives explicit owner/maintainer/publisher authority. PackageProject has one current publisher
owner and retained ownership history. A key identifies an artifact signer, not an account or
local trust policy. Any future verified-publisher label must name its evidence (account,
domain or organization proof); it must not imply safe code, local trust or sandbox exemption.

## 14. Namespace ownership

One project owns each exact canonical G package ID; claims are unique and race-safe. Claiming
one dotted ID grants no prefix namespace and proves no domain ownership. Published IDs are
never recycled. Controlled onboarding is permitted for pre-H rollout. Transfer requires
explicit authorization by current and destination ownership, recent authentication, a
transactional race check and audit; suspension retains identity and history.

## 15. Signing-key lifecycle

Store PUBLIC Ed25519 keys/fingerprints only; private signing keys stay developer/CI-owned.
Keys are active, retired or revoked. ProjectKeyAuthorization separately establishes which
key can publish for which project. Retirement prevents new use while retaining attribution;
revocation blocks future publication and retains history. Rotation adds a new key and never
rewrites historical signers/bytes. Registration may require proof of possession without
changing G's signature format. Key revocation is distinct from release revocation and H trust.

## 16. Publisher authentication requirements

Use an external OIDC-capable identity model and independent hosted sessions/security domain.
Require recent authentication for sensitive account, membership, key and project operations.
CLI publication credentials are short-lived, narrowly scoped, revocable and independently
authenticated through a reviewed native-app-safe flow. No local session-cookie reuse,
permanent broad token by default or plaintext credential fallback. If persistence is needed,
use an OS credential store. I2 chooses provider, protocol and library; none is frozen here.

## 17. Conceptual data model

These are invariant-bearing concepts, not I0 tables or exact schema prescriptions.

| Entity | Canonical identity | Mutability/history | Publication relationship |
| --- | --- | --- | --- |
| MarketplaceAccount | Hosted account ID | Mutable profile; disable/tombstone retained | Actor, not package signer |
| ExternalIdentity | Exact issuer + subject | Audited association/removal | Authentication evidence |
| Publisher | Hosted publisher ID | Mutable profile; suspension retained | Owns projects |
| PublisherMembership | Publisher + account | Audited role changes | Current write authority |
| PublisherSigningKey | Publisher + key fingerprint | Public bytes fixed; lifecycle retained | Historical signer association |
| ProjectKeyAuthorization | Project + key | Audited grant/retirement | Publication permission |
| PackageProject | Exact G package ID | Ownership/listing pointers mutable; no recycling | Parent of exact releases |
| PackageListing | Project ID | Revisioned presentation/moderation | Cannot override signed semantics |
| UploadOperation | Scoped operation ID | Durable processing state; expiry/history | Candidate artifact, never public by itself |
| PackageRelease | Package ID + exact version | Immutable evidence; distribution state separate | One published artifact forever |
| Artifact | Archive SHA-256 | Immutable bytes/length/content/signer facts | Content-addressed release evidence |
| PackageAsset | Asset digest + project association | Immutable bytes; listing association mutable | Bounded safe presentation |
| Category/Tag | Catalog identifier | Managed presentation metadata | Search/filter only |
| SecurityAdvisory | Advisory ID + revision | Versioned history/status | Observations about exact releases |
| PublicationAuditEvent | Durable event ID | Append-only mutation evidence | Actor/action/identity, no secrets |
| Publication credential/session | Scoped hosted credential identity | Expiry/revocation; hash/redacted evidence | Hosted authentication only |

Disabling accounts/publishers cannot cascade-delete published artifacts, releases or audit.
Current authority is rechecked transactionally at sensitive mutations, including publication.

## 18. Signed versus presentation data

Executable semantics come only from the exact `.nervos` artifact: ID/version, entrypoint,
configuration schema, capabilities, compatibility, signed payload and integrity evidence.
Mutable listing summaries, categories/tags, screenshots, profiles, links, featured state and
possible license claims cannot override them. Extracted README content remains untrusted.
Disable raw HTML, scripts and iframes; initial V1 excludes SVG absent reviewed sanitization.
Do not automatically fetch/embed remote tracking images. PNG/JPEG/WebP assets require bounded
count, bytes, dimensions and safe decoding. Links use safe schemes and user navigation,
not server-side fetching. I1/I3 review rendering and operational limits.

## 19. Immutable release contract

Published `(package_id, package_version)` maps forever to exact archive bytes, archive SHA-256,
content digest, signer fingerprint and signed manifest/payload. Identical retries may return
the same release. Any different immutable evidence at that version is a hard conflict,
including another valid signer. No replacement or in-place code edits; new code needs a new
SemVer version. Listing edits, transfer, moderation and key lifecycle cannot rewrite evidence.

## 20. Upload/publication state machines

UploadOperation: `uploading → uploaded → verifying → verified`; exceptional outcomes are
`rejected` (invalid package/policy), `failed` (infrastructure failure) and `expired` (abandoned
bounded operation). Retry of infrastructure work uses durable identity and bounded recovery;
it cannot turn an invalid candidate into a public release without successful verification.
PackageRelease: `ready → published`; only published releases enter public catalog.
Distribution: `available ↔ yanked → revoked`; available may also become revoked directly.
Revoked is monotonic in V1. Upload, publication, distribution and listing moderation are
separate vocabulary and authority. Verification alone does not publish.

## 21. Publication idempotency

Scope write idempotency to authenticated actor/project/operation and immutable request
evidence. Repeat identical publication returns the existing exact release; conflicting
evidence fails closed. Unique database identity and conditional immutable object writes
resolve races. No token format is frozen. Recheck current membership, project ownership and
key authorization at publication, even if upload/verification happened earlier.

## 22. Crash/recovery matrix

PostgreSQL and object storage do **not** form one ACID transaction. Durable operation state,
immutable object identity, idempotency and reconciliation converge without serving unverified
or substituted bytes. Never infer publication merely from an object existing.

| Failure boundary | Required recovery |
| --- | --- |
| Partial upload/no row | Bound and expire quarantine; never discoverable |
| Object/no DB row | Reconcile by digest/operation or garbage-collect after retention; never infer a release |
| DB operation/object missing | Mark infrastructure failure; re-upload identical evidence or expire |
| Verification crash | Recover bounded work from durable operation; no publication before complete verification |
| Verified object/release not reserved | Recheck authority; atomically reserve exact identity or conflict |
| Object finalized/publication transaction absent | Retry/reconcile same evidence; object remains private |
| Published row/object unavailable | Fail download closed; report availability incident; restore only identical bytes |
| Same artifact concurrent publication | One release; return identical result to the loser |
| Different artifact/same version | Unique reservation wins; loser hard conflict, no overwrite |
| DB/object-store restore skew | Reconcile immutable evidence; withhold mismatched/missing distributions; never manufacture new bytes |
| Audit write failure | Roll back hosted mutation transaction; no unaudited publication/ownership/status mutation |

I2 must specify retry budgets, operation expiry, restore procedures and reconciliation tests
without creating local Runs/Jobs. Unreferenced storage cleanup must preserve published history.

## 23. Publication audit

Namespace claim, transfer, membership changes, key registration/retirement/revocation,
publication, yank, release revocation and publisher suspension require hosted audit in the
same database transaction as their authoritative mutation. Audit records actor, target,
immutable evidence/revision, action and time; no private keys, credentials or local secrets.
Hosted PublicationAuditEvent is separate from local Run Events and confers no execution power.

## 24. Artifact storage

Private immutable objects use archive SHA-256 addressing, never raw package-ID filesystem
paths. Finalization uses conditional creation and equality checks, not mutable overwrite.
S3 ETag is not a SHA-256 guarantee. Public delivery is service-controlled and status-checked.
Preserve or tighten current G limits: 256 MiB archive, 1 GiB extracted data, 20,000 entries,
256 MiB single file. Hosted resource and concurrency limits must account for verifier memory
usage as well as streamed transport. Safe assets are independently bounded. No hosted builds.

## 25. Static Stage-G verification reuse

Reuse `nervos_core.application.package_verification.verify_package` and existing G contracts,
not a second parser or signature scheme. Check ZIP/path/layout safety, manifest/identity/
SemVer, config schema, compatibility declarations, wheel metadata/offline closure,
`integrity/files.json`, content/archive digests, Ed25519 signature and fingerprint. Then check
hosted project/key authorization. Verification cannot import wheels, execute entrypoints,
pip-install, launch package-host, perform activation health checks, or call providers/tools.
Compatibility display is declaration-based; local activation remains authoritative.

## 26. Hosted public API semantics

Versioned Marketplace protocol exposes bounded search/list, package detail, versions, exact
release detail, exact artifact download, publisher detail and advisory/status revisions.
Target route family `/marketplace/v1` is separate from local `/api/v1`; I1 may refine route
names while preserving semantics. Bound query bytes, page size and cursor traversal; stable
keyset pagination, validation and explicit error contracts avoid unlimited work. No arbitrary
URL proxy. Numeric tuning is reviewed in implementation; security/correctness G bounds remain.
Exact-release descriptors include ID/version, digests, signer, length and freshness/status
observation; they are claims until independent local verification.

## 27. Publisher API semantics

Hosted writes cover publisher/project management, namespace claim/transfer, key challenge/
registration/retirement/revocation, upload operation/bytes/status, publication, yank/advisory/
revocation and safe listing/assets. Authentication, scope, membership, ownership and audit are
server-side. Upload and publication remain distinct; failed/unverified uploads never become
public. Publisher CLI talks directly to hosted service with hosted credentials; local package
CLI continues using local API. Future command names are target UX, not current commands.

## 28. Search/discovery

V1 searches ordinary metadata: exact package ID, name, summary/description, publisher,
category and tags. PostgreSQL-native indexing/FTS initially; I1 selects bounded ranking and
pagination. No vector store, LLM search, personalization, ratings or recommendation engine.
Default search excludes yanked/revoked releases as ordinary install candidates. A displayed
latest version is convenience only; all download/install operations resolve exact versions.

## 29. Marketplace origin/network security

V1 uses one **operator/admin-configured** Marketplace origin. Public hosted origins require
exact HTTPS. An administrator may explicitly configure private/self-hosted network HTTPS;
loopback HTTP is permitted only in explicit development/test mode. Ordinary user/browser
input never supplies an origin, artifact URL or destination override. Reject URL credentials,
query and fragment. Fail closed on redirects; V1 exact artifact delivery stays on the
configured origin, without arbitrary CDN/presigned-host redirects. Validate DNS/address and
origin policy at actual connection time, including rebinding; private addressing requires
the explicit administrative mode. Proxy credentials/environment inheritance must not widen
authority. The primary SSRF defense is fixed configuration + no untrusted URLs + strict
origin/redirect controls. Private self-hosting remains possible. TLS certificate validation
must not be silently disabled. I3 tests public, private and development modes separately.

## 30. Local client dependency boundary

Core may own provider-neutral application ports/value types where needed. Concrete HTTP,
JSON/TLS transport stays outside core and is composed at the local API boundary. Worker,
Scheduler, SDK and package-host acquire no Marketplace dependency. `packages/nervos-marketplace`
is a candidate, not a mandated package; I3 source-inspects the then-current workspace and
chooses the smallest clean adapter placement. I0 creates no adapter or workspace member.

## 31. Browser network path

Browser → authenticated local API → configured client → hosted Marketplace. Preserve local
opaque-session authentication, ownership checks and Origin protection for mutations.
Never expose publisher credentials or an arbitrary proxy to the local browser. Hosted reads
receive no local cookies; UI cache/logout hygiene follows existing private-query patterns.

## 32. Exact download

Obtain a fresh exact descriptor with availability observation before new Marketplace download.
Stream bounded raw archive bytes into private staging; enforce time/resource/length limits
and expected archive SHA-256; reject truncation, excess bytes, redirects and digest mismatch.
Avoid transparent encoding transformations that change hashed bytes. A download identifies
package ID, exact version, content digest, archive digest, signer, length and observed status.
Neither latest resolution nor cached presentation can substitute another artifact.

## 33. Local verification/install handoff

Run current G verification locally; compare verified ID, version, content/archive digests and
signer with the descriptor; show **locally verified** facts and pre-H warning. Obtain explicit
local authorization of exact signer/content and archive evidence. Use the same
`PackageApplicationService.install` as manual archives: its snapshot/reverification,
registry, offline environment, local health check and ACTIVE lifecycle remain sole authority.
Marketplace metadata cannot authorize activation. Failure cleanup and existing digest-conflict
behavior remain G-owned. No parallel installer or Marketplace-specific executable resolver.

## 34. TOCTOU and retained install request

Future I4 uses a bounded private install-request/ticket binding authenticated local owner,
configured origin, ID/exact version, content/archive digests, signer, size, retained staging
artifact, expiry and status. Approval and installation refer to the same bytes, never a second
latest lookup. Current `PackageQueryService.inspect_artifact` discards its inspection snapshot;
it alone is not a retained ticket. I4 must retain verified bytes safely, then let the existing
installer snapshot/reverify them with archive authorization. Enforce ownership, expiry,
single-operation/retry semantics and cleanup. Recheck availability before handoff; status
changes require warning/refusal under frozen distribution semantics. No atomic transaction
with hosted status is claimed; record the observation race honestly. Durable successful
install/provenance reconciliation must match exact installed evidence after local crashes.

## 35. Install versus AgentInstance

An ACTIVE install creates node-global software, not an AgentInstance. Separately explicit
configuration may create an instance using existing services. Install never chooses model/
provider, grants tools, binds MCP, creates schedules/webhooks/triggers or writes memory.
Product “one-click install” means entering this reviewed local authorization flow.

## 36. Update/rebind

Availability of a newer version is metadata. User inspects and authorizes the exact new
artifact, installs side-by-side, then optionally explicitly rebinds via G with configuration
validation and capability-delta review. Existing instances keep their binding until changed;
queued/running/retryable/historical Runs retain accepted executable/configuration snapshots.
No automatic upgrades/rebinds and no history mutation.

## 37. Rollback/uninstall

Reuse G: rollback explicitly changes future binding to an already installed compatible
version. It does not rewrite Runs or reverse external side effects. Uninstall remains
obligation-aware removal through PackageApplicationService, honoring bound instances and
execution obligations. Marketplace distribution state cannot directly remove local software.

## 38. Yank/revocation/advisory

Published means public immutable release history. Available permits ordinary download.
Yanked excludes ordinary selection while retaining history; exact access needs strong warning
and explicit acknowledgement. Revoked blocks hosted distribution and retains immutable history.
Advisories are versioned observations. Publisher/key suspension and release revocation are
distinct actions. Pre-H metadata never automatically deletes packages, disables instances,
cancels Runs, revokes D grants, changes local trust or rewrites history. H may define enforcement.

## 39. Cache and freshness

Presentation metadata may be cached for a bounded operational period with `observed_at`,
origin/exact-version identity and explicit fresh/stale state. No arbitrary fixed TTL is frozen;
I3 reviews TTL, eviction, clock handling and invalidation. Cache is neither execution nor
installation authority. Stale observations cannot bypass fresh availability checks for new
Marketplace downloads, local verification or exact authorization. Show unavailable/unknown
freshness honestly; no authoritative security claim from an expired cache.

## 40. Offline behavior

Outage can block browsing, publishing, new remote downloads and fresh update/advisory checks.
It must not block installed execution, Worker claims, Scheduler, tools/MCP, conversation/history,
memory, manual local `.nervos` installation or rollback among installed versions. Worker never
queries Marketplace; installed digests and G snapshots remain executable truth.

## 41. Stage-D/MCP portable capability limitation

Current G machine-enforced requirements support portable built-in upstream tool names.
Node-local MCP connection IDs are not portable Marketplace identities. Listings/manifests
never grant tools. Arbitrary GitHub/email packages needing user-specific external MCP cannot
be advertised as having portable machine-enforced external bindings under current G.

## 42. Future Stage-G capability-binding seam

**STAGE G ARCHITECTURE CHANGE REQUEST — PORTABLE EXTERNAL CAPABILITY BINDING** is a documented
future prerequisite, not an I0 implementation or hidden I1–I5 scope. It does not block core
publication/distribution. Proposed future seam: signed portable external requirement → explicit
owner-local compatible tool/MCP binding → separate D grant → Run-pinned binding evidence where
required → live D permission. Binding is not ALLOW. Separate external G review must precede
changes to frozen manifest/ADRs; this task leaves them untouched.

## 43. Stage-H integration

I can expose typed observations: origin, publisher/evidence, signer, ID/version, digests,
distribution status, advisory revision and observed time. H decides whether these affect
persistent local trust, execution admission, interactive approval, secret policy, sandbox,
network/filesystem/resource rules or local revocation enforcement. Observations do not become
trust automatically. I-before-H is bounded distribution, not hostile-code isolation.

## 44. TUF, Sigstore and OIDC

G Ed25519 remains mandatory artifact signing. V1 uses HTTPS, exact identity/digests, local
signature verification and explicit authorization; **TUF is deferred in V1**. A stale or
compromised metadata channel can hide newer releases/advisories, replay old valid releases or
misrepresent listings; HTTPS and valid artifact signatures do not prevent those attacks.
Before automatic updates, automatic rebind, local revocation enforcement, metadata-derived
execution policy or any autonomous security-critical Marketplace metadata action, adopt a
reviewed authenticated repository-metadata design. Evaluate **TUF** first; do not invent a
custom repository-signing protocol. Sigstore/in-toto may later supplement provenance and
transparency; it never replaces G signing. Future CI OIDC credentials are short-lived and
bound to publisher/project/repository/workflow claims; signing keys remain developer/CI-owned.
No TUF/Sigstore/CI-publishing implementation or dependency in I0.

## 45. Threat model

| Threat | Stage-I mitigation | H responsibility / residual risk |
| --- | --- | --- |
| Malicious publisher | Static validation, attribution, warnings, explicit exact approval | H sandbox/admission; valid malicious code remains possible pre-H |
| Compromised account | Recent auth, scoped credentials, role/key checks, audit | H local trust; authorized abuse may publish new signed releases |
| Compromised signing key | Revoke future publication, advisories, immutable evidence | H enforcement; previously signed malicious bytes may remain valid |
| Compromised hosted DB | Local independent verification, immutable digest comparison | H trust; forged listing/status or old valid metadata remains possible |
| Compromised object store | Conditional immutable writes, digest/signature checks | H containment; deletion/denial and exact valid replay remain possible |
| Same-version replacement | Unique identity, hard evidence conflicts, local G checks | No replacement allowed; privileged hosted compromise can deny service |
| Rollback/freeze attacks | Exact version/age disclosure, manual authorization | Mandatory metadata-design gate; no V1 cryptographic freshness guarantee |
| Archive bombs/malformed data | G bounds/static parser, bounded infrastructure work | H runtime resources; verification itself needs measured resource controls |
| README/listing XSS | Safe Markdown/assets/schemes, no raw HTML/tracking fetch | Rendering defects still need regression tests; sandbox does not fix XSS |
| Download SSRF | Fixed admin origin, strict redirects and actual connection validation | Explicit private origin has intentional network reach; no user override |
| Credential theft | Hosted separation, short scope/lifetime, OS store, redacted audit | H local secrets; compromised publisher endpoints remain a risk |
| Namespace takeover | Unique claims, reserved IDs, no recycling, audited current authority | Social/account compromise and lookalike spelling remain risks |
| Transfer abuse | Both owners authorize, recent auth, transactional race checks | H does not automatically trust new owner; human approval can be deceived |
| Stale revocation/advisory | Observation age/status, fresh check for new download | H local enforcement plus authenticated metadata design later |
| Marketplace outage | Offline local lifecycle; fail new remote operations closed | Provider/tool availability remains independent |
| Malicious Marketplace | Independent G signature/digests/exact approval, no runtime push | Can withhold or misrepresent valid artifacts; signature is not safety |
| Hostile package code pre-H | Honest warning and explicit local activation decision | H containment/secrets/resource protection remain undelivered |

## 46. Testable security invariants

No hosted package execution/builds; no second parser/signature; no same-version replacement;
no private-key upload; no local runtime secrets/telemetry; no arbitrary remote URL; no listing
override of executable truth; no Marketplace-derived grants or implicit trust; no automatic
instances, triggers, memory, upgrades or rebinds; no hosted push to runtime; no Marketplace
dependency in Worker/Scheduler/SDK/host; static/signature validity is never “safe package”.

## 47. Cross-platform requirements

Preserve G portable format/path/wheel/Python constraints across Windows and supported Unix
platforms. Private staging, permissions, atomic same-volume publication, cleanup, file locks
and disk exhaustion need native-platform tests. Local CLI credential persistence uses OS
facilities without plaintext fallback. Source paths never derive from untrusted package IDs.
I4 must prove retained-artifact safety on Windows as well as Unix; no shell-dependent downloads.

## 48. Migration separation

I0 adds no migration, table or reserved revision `0014`. Local head remains `0013` with
unchanged Worker/Scheduler guards. Hosted I1 starts its own Alembic configuration/history for
catalog/releases/listings/artifacts; I2 adds identity/membership/key/upload/audit/advisory
concepts as needed. I4 evaluates a small local migration for durable install requests and
origin/status observations only if required, discovering the then-current head. Such records
remain separate from Run/Job/Attempt and executable-resolution truth; exact G digests remain
authority. Do not infer Marketplace origin for existing manual installations.

## 49. Testing strategy

Use units for contract/state/security boundaries, PostgreSQL/object-store integration for
concurrency/conditional writes/recovery, local isolated SQLite/API integration for exact
handoff, safe UI behavior tests and a small supervised deterministic E2E journey. Never use
the developer's runtime DB or live LLM/production Marketplace credentials. Include malicious
archives/listings, authority races, restore skew, digest mismatch, timeout, expired tickets,
origin/redirect modes, offline baseline and unchanged manual install. Each milestone runs
repository lint/typecheck/security/full check and E2E. I0 adds only one file-presence/protocol
guard consistent with G precedent; no Marketplace behavior tests before implementation.

## 50. I1–I5 milestones

| Milestone | Scope | Exclusions |
| --- | --- | --- |
| I1 — Hosted Marketplace Foundation and Immutable Catalog | Activate hosted service; separate persistence/history; artifact abstraction; package/release/listing foundation; bounded public reads | Publisher upload, local client/install and execution |
| I2 — Publisher Identity and Publication Workflow | Hosted auth/roles/ownership/keys; signed upload/static G verification; immutable publication; moderation/status/audit; publisher CLI/API; recovery | Hosted execution, local trust/install |
| I3 — Local Discovery Client and Marketplace UI | Configured origin; typed adapter; local browse/search proxy; safe UI; version/status/compatibility; privacy/outage | Package mutation/install |
| I4 — Exact Marketplace Download and Stage-G Install Handoff | Bounded exact download; retained request; independent verification/authorization; existing installer; provenance if needed; update/rebind integration; status warnings | Automatic trust/updates/grants or H enforcement |
| I5 — Integrated Acceptance and Closeout | Complete deterministic journey, v2/rebind/pinning, warnings, offline execution, crash/security regressions and documentation | New future-stage scope |

## 51. Acceptance matrix

| Milestone | Required proof before acceptance |
| --- | --- |
| I0 | Master + four ADRs and accurate mutable docs; governance guard; predecessor hashes unchanged; no runtime/dependency/migration; full check/E2E/security; external review pending separately |
| I1 | Separate hosted migrations/PostgreSQL contract; immutable artifact abstraction/identity; bounded reads; no hosted local-runtime tables, execution or publisher publication path |
| I2 | Auth/current membership/ownership, namespace conflict/transfer races, public-key lifecycle/authorization, invalid rejection, private keys absent, identical retry/conflicting retry, crash matrix and mutation/audit atomicity; no code execution |
| I3 | Browser through local API, configured public/private/dev origins, secure transport/redirect boundary, safe rendering, bounded queries, privacy, no arbitrary proxy, outage-safe runtime, no package mutation |
| I4 | Exact version/size/digests, independent G comparison, retained owner-bound artifact and explicit approval, sole G installer, unchanged manual install, install != instance, side-by-side explicit rebind, historical pinning, no silent revocation deletion |
| I5 | Sign/publish v1 → static verify → discover/exact download → local verify/approve/install ACTIVE → explicit instance → Run/Job/Attempt/Worker/PackageExecutionAdapter/package-host/AgentResult; publish/install v2 while instance stays v1 → explicit rebind/new v2 Run → v1 history intact; yank/revoke warning without local mutation; stop Marketplace and run installed agent; recovery/security/full gates |

All future acceptance requires evidence and external review under existing project practice.

## 52. Explicit non-goals

No billing/payments/subscriptions/paid agents/revenue sharing, ratings/reviews/social feed,
personalization/LLM/vector search, automatic updates/rebinds/grants/triggers/memory, registry
federation, package-to-package dependencies, hosted builds/agents/remote model execution,
secret sync, H sandbox/persistent trust, J collaboration or K IoT in I1–I5.

## 53. Future extensions

Separately reviewed seams include multiple registries (digest conflicts fail closed), trusted
CDN origins, TUF, Sigstore/in-toto, CI OIDC, domain proof, SBOM/vulnerability scanning,
isolated malware analysis, portable external binding, H enforcement, paid/community features
and dedicated search infrastructure. They are not implicit milestone authorization.

## 54. Architecture-change protocol

Conflicts with this contract require **STAGE I ARCHITECTURE CHANGE REQUEST — <issue>** before
implementation. Changes to G format, identity/SemVer/signatures, execution pinning or
PackageApplicationService lifecycle require **STAGE G ARCHITECTURE CHANGE REQUEST — <issue>**.
Do not silently edit frozen predecessor plans/ADRs. H/F/D/E authority changes require their
own review. I0 final documents remain pending external acceptance; report the candidate blob
twice externally, leave changes uncommitted, and do not start I1 without authorization.
