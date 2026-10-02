# Stage I2 + I3 verifier hardening and acceptance report

**Historical report:** the current I4/I5 worktree completes the reachable MVP
publication/download/install/runtime path described as unfinished below. Current
evidence and remaining production acceptance are in `i4-i5-implementation.md` and
`../implementation-status.md`. Historical measurements and failures are retained.

Recorded 2026-10-02. **Work in progress.** The user authorized Requires-Dist
budgets, then chose streaming PEP 440 normalization to preserve Version acceptance.
Both are implemented. The 255 MiB local-Version fixture now passes on Windows
and Linux (about 69 seconds wall; under 1 GiB commit/virtual memory).
Core units: **1,482 passed**. Stage-G integration: **14 passed**. Final-source
resource qualification, including combined pressure, is still running.
Historical results below are checkpoints, not final acceptance. I2 remains
partial/unaccepted and I3 not started.
Delivered milestones remain I0/I1. The earlier report is preserved in
[the storage history](i2-i3-storage-resolution-2026-10-01.md).

## 1. Starting state

Branch stage-i; HEAD and origin/main both 9b0756e102b671da8132ea2a5a81ddc39b56ad9f. Existing identity/publication/migration/OIDC/storage drafts were preserved. No Git finalization was authorized.

## 2. Root cause

The old BytesParser materialized ignored headers/body. A 255 MiB ignored-header input exhausted an 8 GiB builder Job Object: commit 8,392,601,600 bytes, RSS 3,962,327,040 bytes, before a signed package existed. Streaming removes that amplification. Requirement marker trees required the approved admission budgets. Another amplification source was Version's retained local/release segments; chunked normalization now avoids those lists and tuples.

## 3. Consumed-field inventory

| File | Field | Preserved behavior |
| --- | --- | --- |
| METADATA | Name | Exactly one; PEP 503; dependency filename comparison |
| METADATA | Version | Exactly one; PEP 440 |
| METADATA | Requires-Python | At most one; Python 3.12 compatibility |
| METADATA | Requires-Dist | Every occurrence; Requirement syntax, Linux/Windows markers, offline closure |
| WHEEL | Tag | At least one; every value py3-none-any |
| WHEEL | Root-Is-Purelib | Exactly one value, true |

Metadata-Version, Provides-Extra, descriptions/body, classifiers, author fields, unknown headers, Wheel-Version and Generator remain unconsumed.

## 4. Reader implementation

package_wheel_metadata.selected_headers consumes bounded archive chunks, discards irrelevant headers/body and drains the stream for CRC/size validation. It preserves compat32 boundaries, continuations, defects and Header objects for non-ASCII bytes. Single-use duplicate witnesses need at most two values. Short-value sharing retains every multi-use occurrence. Public requirement tuples remain observable and selected evidence is still retained: this is partial hardening, not proof that every consumed field is resource-safe.

## 5. Equivalence

The differential corpus covers normal fixtures, mixed case, duplicates, folding, LF/CRLF/CR, malformed transitions, unknown fields, UTF-8, descriptions and 400 deterministic malformed messages. Chunk sizes include 1/31/65,536 bytes. Ordinary signed output is byte-identical to the old parser.

Large Requires-Python conjunctions retain SpecifierSet syntax and error precedence. Bounded Requirement caches preserve original invalid positions and complete-parser fallback. The shared-marker shortcut covers only a conservative bare alphanumeric name with a short suffix validated by Requirement; applicable dependencies receive the full closure check. Tests compare malformed markers, platform cases, extras, URLs, folding and cache overflow with original behavior.

Current core unit command: uv run pytest packages/nervos-core/tests/unit -q:
**1,544 passed in 22.75 s**. Version differential tests include 1,080 generated
grammar combinations, 4,000 deterministic malformed strings, aliases, Unicode,
numeric conversion errors, large release/local values and cache overflow.
Three large-version build/verify cases prove signed bytes and evidence match
the original packaging.Version normalization.

Final focused G integration command selected package installation, lifecycle,
registry, rebind concurrency and G3/G4/G5 acceptance files explicitly:
**14 passed, 7 SQLAlchemy/SQLite dependency deprecation warnings, 18.10 s**.
These existing acceptance journeys pass; they do not replace the new verifier
resource qualification gate.

## 6. Ignored-field pressure: 64 / 128 / 192 / 255 MiB

Final-source Windows 64/128/192/255 MiB checks pass. Peak commit is
21,835,776 / 22,093,824 / 22,327,296 / 22,286,336 bytes; CPU is
4.500 / 8.156 / 12.047 / 15.625 seconds; wall is
4.485 / 8.031 / 12.062 / 15.765 seconds. Old 64 MiB RSS was 2,679,193,600 bytes.
The Linux size matrix also passes; the 255 MiB case uses 54,407,168 bytes
virtual memory and 13.614 seconds wall. Dedicated final-source files and
[historical evidence](evidence/verifier-resources.json) retain every measurement.

## 7. Relevant-field pressure

These fixtures are signed by the real G builder. Their agent module raises if imported; the verifier does not import it. Builder ceilings are fixture-generation limits, distinct from the unchanged verifier limit of 2 GiB/120 CPU and wall seconds.

| Windows fixture | Verifier commit bytes | CPU s | Wall s | Outcome |
| --- | ---: | ---: | ---: | --- |
| 255 MiB unique names/shared inactive marker | 1,068,011,520 | 76.828 | 134.156 | Exit 0 but outside wall criterion; unqualified |
| One 4 MiB OR marker | 137,711,616 | 8.844 | 8.843 | Passed |
| One 32 MiB OR marker | 935,710,720 | 67.844 | 68.984 | Passed |
| One 64 MiB OR marker | Final snapshot unavailable | Unavailable | 120, forced termination | Failed |

The 64 MiB fixture was fully built and signed: builder exit 0, commit 1,845,403,648 bytes, RSS 1,854,373,888 bytes, CPU 253.109 s, wall 258.109 s. Actual METADATA size 67,108,951 bytes, one Requires-Dist occurrence; signed archive 166,980 bytes, SHA-256 4ec796838aa0778537610da1851833c085868483f4e0752e9ce36000feec7490. Builder peak memory is **not** the terminated verifier's peak. The verifier's final snapshot is unavailable.

The preceding table is pre-policy history. The approved Requires-Dist bounds
now reject its oversized cases early. Maximum 4 MiB aggregate and 100,000
occurrence fixtures pass on both platforms. Final-source 255 MiB folded Name
and Requires-Python fixtures also pass. Version streaming uses packaging's
public VERSION_PATTERN and suffix normalizer, preserving syntax/error behavior
without a new Version cap. A 64 MiB local Version previously built successfully
at 2,528,821,248 bytes commit but failed the 2 GiB verifier; it now passes at
259,547,136 bytes commit and 16.875 seconds wall. The 255 MiB local Version
passes Windows at 962,371,584 bytes commit and 69.203 seconds wall; Linux at
923,148,288 bytes virtual memory and 69.160 seconds wall. Large release and
single-local-segment cases pass too. Combined/WHEEL pressure is still running;
no complete resource gate is claimed yet.

## 8. Windows boundary

Real Job Object memory/CPU/one-process/kill-on-close limits are implemented. Earlier initialized negative memory/CPU/descendant/wall tests passed. Working set is not committed memory. Positive qualification remains incomplete.

Final Windows negative probe: exit 0, all four modes passed after LIMITS_READY.
Memory exit 2 (0.250 s), CPU exit 3221225540 (7.656 s), descendant exit 2
(0.266 s), wall termination (1.031 s). Raw results are retained in machine evidence.

## 9. Linux boundary

Docker was restored after the user's action. Final-source positive size,
Requires-Dist, folded-Name, Requires-Python and Version cases pass under UID 65534,
dropped capabilities, disabled network and RLIMIT_AS/CPU. Fresh negative
memory/CPU/descendant/wall tests pass. The measurement helper now uses Linux
VmHWM for resident peak: ru_maxrss can retain a parent's pre-exec watermark.
Older records are retained and not silently relabeled. Remaining combined
pressure is required before full Linux qualification is claimed.

## 10. Time

Measured verifier CPU/wall stay 120 seconds. The diagnostic checks reported wall
time as well as exit status, so a late exit 0 cannot pass. Builder deadlines do
not extend verifier deadlines. The dedicated child scratch directory is sampled
every 50 ms; current positive cases observe zero scratch bytes. This is a sampled
peak, separate from fixture-generation disk. The verifier streams archive reads,
imports no agent code and performs no extraction; bytecode writing is disabled.
Full profile selection remains pending combined/aggregate pressure qualification.

## 11. Stage-G compatibility

Identity, SemVer, archive maxima/layout, manifest, lock format, digest, Ed25519
signature, lifecycle, execution pins and SDK contract are unchanged. Differential
ordinary and large-Version output is byte-identical. Frozen G remains unchanged.
Requires-Dist acceptance is intentionally narrowed by user-authorized ADR 0031;
Version acceptance is preserved by the explicitly selected streaming alternative.

## 12. Classification and required review

The reader/cache/Version changes are implementation hardening. The separately
user-authorized Requires-Dist admission amendment is recorded in ADR 0031.
Resource qualification remains incomplete.

**STAGE G ARCHITECTURE CHANGE REQUEST — BOUNDED WHEEL CORE METADATA**

The user's verifier-blocker brief, sections 8/20, required this request before
semantic limits. The user then explicitly chose to implement the proposed limits,
requalify and resume I2/I3. Shared enforcement is now present; frozen plans remain
unchanged. [The request](verifier-metadata-review-request.md) preserves the evidence
and compatibility consequences. The separate Version review was answered with
authorization to implement streaming normalization; no additional Version cap
is pending or implemented. No milestone is declared complete.

## 13. I1 fixture correction

Unchanged, as mandated until resource qualification passes. The four-table cleanup conflicts with draft mp0003 upload-operation foreign keys. The later fix must clean the complete disposable schema without weakening constraints or pinning tests back to mp0001.

## 14. I1 integration

The corrected disposable reset now disables only the two production append-only guards while
truncating the test-owned database, then restores them. The fixture supplies explicit publisher
attribution and exact S3 version IDs. `uv run python scripts/marketplace_integration.py` now passes
**50 tests in 35.95 s** on PostgreSQL 18 and SeaweedFS, including the real-server smoke path.

## 15. I2 completion

The MVP now composes the existing OIDC, opaque-session, CSRF, authorization and publisher services
into hosted routes for login start/callback, publisher creation/listing and project claims. MVP
onboarding activates a publisher immediately; production moderation remains a later hardening step.
Upload, quarantine, verifier-parent worker, leases/fencing/reconciliation, publisher CLI and real
OIDC acceptance remain unfinished. Exact-version storage remains qualified.

## 16. I2 acceptance

The hosted integration suite passes **50 tests** and the Marketplace unit/architecture suite passes
**113 tests**. The MVP publisher routes still require a configured external OIDC provider for a
real login journey, so I2 remains unaccepted for production.

## 17. I3 implementation

An MVP local discovery slice is now present behind the existing authenticated local API boundary.
`NERVOS_MARKETPLACE_ORIGIN` configures one exact hosted origin; the client disables ambient proxy
discovery, follows no redirects, sends no local identity, and exposes bounded package search at
`/api/v1/marketplace/packages`. The web dashboard has a Marketplace page and a Dashboard link.
Artifact download, installation, publisher UI, cache persistence and production SSRF/DNS/TLS
qualification remain intentionally deferred to later I3 hardening/I4.

## 18. I3 security

The MVP validates a single configured origin, disables proxy environment variables, rejects
redirects and bounds response time and query length. Connection-bound DNS/TLS, SSRF address
classification, persistent cache and outage acceptance remain required before production I3
acceptance.

## 19. I3 acceptance

Not run. No discovery success or skipped acceptance is implied.

## 20. Combined Marketplace E2E

Not implemented/run. Publish-to-discover requires completed I2/I3.

## 21. Full repository check

Historical BEFORE G hardening: scripts/check.py check exit 0, 3,035 Python passes/50 deselected opt-in tests, 152 frontend passes. This is not final-tree acceptance. Repeat after blocker resolution and implementation.

## 22. Existing E2E

Historical BEFORE G hardening: scripts/check.py e2e exit 0. No fresh final-tree or Marketplace E2E pass is claimed.

## 23. Static/security

Final Ruff check/format passed (551 files); Pyright 0 errors/warnings; security scan 671 files/no findings; lock check 109 packages. Four private-helper uses in new tests were corrected to public APIs; Pyright then passed. No check or assertion was weakened.

## 24. Migrations

Local/Worker/Scheduler remain 0013. Earlier disposable PostgreSQL storage qualification upgraded mp0001 -> draft mp0002 -> draft mp0003. Accepted hosted startup remains mp0001. Reconciliation, downgrade guards and complete cycles are unfinished. No developer database was migrated.

## 25. Frozen hashes

- F: 62cfc0a8039e233c82e2a30ff3fd59495e99395d
- G: 4554ab5e93b35f4bbc4daed164ee5712077c1006
- I: 7bf5cff40eb7ed5defe2d7598b72e0606d29ce7b
- I repeated: 7bf5cff40eb7ed5defe2d7598b72e0606d29ce7b

Frozen plans and ADRs were not edited.

## 26. Scope audit

No I4/H/TUF/Sigstore, local download/install, implicit trust/grants, package execution or runtime schema change. Sensitive configuration/credentials were not read. Storage acceptance was preserved.

## 27. Git state

Uncommitted on stage-i. No staging, commit, push, PR, merge, reset, clean or history rewrite. Local governance files, graphify data and unrelated changes were preserved. Diff check passed; uv.lock has Git's LF/CRLF advisory. Disposable fixtures were removed.

## 28. Final stage state

I0/I1 complete/externally accepted; I2 and I3 MVP slices are implemented but unaccepted; I4/I5/H
are not started. No production memory ceiling or headroom is accepted.

## 29. Final result

The approved Requires-Dist change and selected Version streaming implementation
are present and their regression checks pass. Final resource qualification is
running; I1 cleanup and I2/I3 implementation remain ordered after that gate.

STAGE I2 + I3 COMBINED MVP BLOCKED — production OIDC/publication acceptance and hardened I3 transport/combined acceptance remain incomplete
