# STAGE I2 + I3 BLOCKER-RESOLUTION AND IMPLEMENTATION REPORT

Recorded 2026-10-01. **Storage refinement qualified; I2 partial, blocked and
unaccepted; I3 not started.** Delivered state remains as recorded in
[implementation status](../implementation-status.md).

## 1. Starting state

Branch stage-i. HEAD and origin/main:
9b0756e102b671da8132ea2a5a81ddc39b56ad9f. I0/I1 were accepted and merged.
Existing I2 migration, identity, authorization, publication, verifier and dependency
drafts were retained. I3 was absent. Frozen F/G/I blobs appear in section 27.

## 2. Original storage blocker

The original disposable probe established:

| Operation | Result |
| --- | --- |
| First conditional PUT | Success |
| Duplicate conditional PUT | 412 |
| Valid conditional PUT with tested s3:if-none-match policy | 403 |
| Unconditional PUT with that policy | 403 |
| Unconditional overwrite without that policy, restricted writer | Replacement read back |

The policy rejected legitimate requests too. Versioning does not repair that
policy. scripts/marketplace_storage_probe.py remains a historical diagnostic;
exit 0 means the diagnostic completed, not that immutability was accepted.
Its typing was corrected without changing those requests.

## 3. Reviewed resolution

New Artifact identity comprises digest-derived key, exact provider VersionId,
expected SHA-256 and size. Conditional creation resolves races; protected
exact-version reads preserve referenced bytes after subsequent writes. No
HEAD/unconditional-PUT substitute was introduced.

## 4. SeaweedFS versioning qualification

The existing image was retained:

    chrislusf/seaweedfs:4.48@sha256:4e61d15fd35994cb1e43e1e553dff106794841fd9a99ade2fc8c8bfce4d7872d

The disposable bucket reported Enabled, including the actual finalizer readiness
check. No backend upgrade/replacement occurred.
[Machine evidence](evidence/storage-versioning.json).

## 5. Versioning live proof

| Evidence | Value |
| --- | --- |
| V1 | 672588b71ff6292ae384d45718bec21c |
| Duplicate conditional request | 412 |
| V2, unconditional PUT by restricted finalizer | 672588b7125411ad3328af344795622c |
| V1 SHA-256 after V2 | d06651f83bfaf4343d9f9d5a92d33bb835e6a9d539fb0c4e33d7b37ce1a97956 |
| V2 SHA-256 | 16cdb544f1205fae92abd955e26ae5a6fa25bd7991eac14276ee4227e331d894 |
| V1/V2 sizes | 25/28 bytes |

V1 remained exact. The latest/default version changed; key-only reads are
insufficient for the new immutability guarantee.

## 6. Storage authorization proof

Synthetic reader: artifacts/* read. Quarantine writer: quarantine/* read/write.
Finalizer: final-prefix write and bucket read for the versioning check; no bucket
write/admin rights. For all three normal identities, direct version/current
delete, versioning suspension, bucket delete, policy mutation, lifecycle mutation
and object-lock configuration returned 403 AccessDenied. Batch deletion returned
HTTP 200 **with per-object AccessDenied and no Deleted entries**. Reader writes,
quarantine final writes and reader quarantine reads were denied.

Additional PUT/Copy attempts targeting the provider version namespace returned
200 with new VersionIds; the original pinned V1 remained byte-identical after
each attempt. They did not offer an alternate overwrite of the pinned leaf.

The final namespace policy denies DeleteObject and DeleteObjectVersion. Privileged
test maintenance temporarily removed it to create a delete marker, then restored
it. These are disposable deployment proofs; no deployed bucket was altered.
Private filer/volume/admin access and expiration-free final storage remain
required deployment controls.

Pinned source corroborates generated versions and rejection of versioned-bucket
renames: [PUT implementation](https://raw.githubusercontent.com/seaweedfs/seaweedfs/4.48/weed/s3api/s3api_object_handlers_put.go),
[rename implementation](https://raw.githubusercontent.com/seaweedfs/seaweedfs/4.48/weed/s3api/s3api_object_handlers_rename.go).
Those source observations are distinct from the live operations above.

## 7. Artifact schema change

Draft mp0003_publication_workflow adds internal artifacts.storage_version_id,
VARCHAR(1024). Its constraint separately checks length 1â€“1024, the allowed
characters, and exclusion of the null placeholder. PostgreSQL rejected the first
{1,1024} regex repetition expression; this was fixed with length(...) plus a
character regex. Six invalid values were rejected and a 1024-character value
accepted in the disposable DB. Version updates were blocked by the existing
immutable-evidence trigger.

Legacy rows remain NULL; no provider ID is invented or inferred. The draft release
guard requires non-NULL version evidence for new releases and ready-to-published
transitions. Existing I1 rows retain full-integrity key-only read compatibility;
they are not described as newly version-pinned. Legacy reconciliation and complete
migration-cycle acceptance remain unfinished, including historical guard
restoration on draft downgrades.

## 8. ArtifactStore contract

FinalizedArtifactRef(archive_sha256, size_bytes, version_id) is a frozen internal
reference. The key derives only from digest. VersionId is opaque query evidence,
never a filesystem path. S3FinalArtifacts.create_final_if_absent returns this
reference after real creation/adoption and complete verification. Publication
storage/reservation drafts carry the reference.

## 9. Version-pinned read implementation

HEAD and GET receive exact VersionId and must return matching version evidence.
Private full staging, length/SHA verification, deadlines, admission and cleanup
remain enforced. Persistence returns internal version evidence separately from
public DTOs. Delivery rechecks status and the reference after staging. The
persistence query uses to_jsonb so the old I1 schema remains read-compatible.

## 10. Corruption/current-version proof

The production finalizer created a signed G fixture; its version was persisted
in real PostgreSQL. A restricted finalizer wrote a same-length corrupt V2.
The real artifact route returned the original bytes, SHA-256:

dc5ffb7083fff4147a3b9928d0919af81617b1f5f46a395e52122ae7b8c247ef

The route still returned exact original bytes after an administrative delete
marker. Public descriptors omitted storage version fields. This was the real
repository/service/router test composition, not a Publisher workflow or production
startup/readiness acceptance. The current startup schema guard remains mp0001.

## 11. Concurrent finalization

Simultaneous conditional requests produced [200, 412]. Two concurrent production
finalizer calls converged to the same fully verified reference, VersionId:
672588b689cc72e47888e61e9a3ebd6b. No historical-version scan was used.

## 12. Existing-object recovery

After 412, recovery observes current VersionId through HEAD, then pins and checks
complete length and SHA-256. Retry adoption returned the same reference.
Same-length corrupt current bytes were rejected. ETags/metadata hashes do not
substitute for full content verification.

## 13. Architecture-change resolution

The externally reviewed reference refinement resolves the original storage
conflict. Package identity, signatures, content digests, Stage-G verification,
SDK/runtime boundaries and PackageApplicationService were preserved. Frozen
plans/ADRs were not edited. Conditional-only policy enforcement remains unsupported.

## 14. Verifier resource proof

**Memory ceiling not qualified.** Signed fixtures built and verified through
unchanged G code on Windows:

| Fixture | Archive bytes | Peak working set bytes | CPU s | Wall s | Fixture directory bytes |
| --- | ---: | ---: | ---: | ---: | ---: |
| Small | 4,150 | 32,935,936 | 0.344 | 0.250 | 6,539 |
| Large archive/nested payload | 262,086,103 | 556,204,032 | 1.266 | 1.187 | 524,170,436 |
| 200 MiB metadata body | 512,885 | 2,161,713,152 | 7.641 | 7.687 | 1,024,006 |
| Combined pressure | 262,106,319 | 2,270,150,656 | 12.344 | 13.000 | 524,210,868 |
| Maximum 256 MiB metadata body | 262,240,573 | 2,766,438,400 | 13.594 | 14.172 | 524,479,376 |
| 64 MiB additional metadata headers | 69,404 | 2,679,193,600 | 66.672 | 67.360 | 137,045 |

The header fixture keeps Name/Version unique and repeats otherwise ignored
metadata fields. Its 64 MiB instance passed the G builder and verifier.
Scaling the same structurally permitted family to 255 MiB plus required headers
stays below G's 256 MiB member limit. But existing wheel inspection failed with
MemoryError under an **8 GiB** Job Object: peak commit **8,392,601,600 bytes**,
working set **3,962,327,040 bytes**, CPU **121.203 s**. Failure location:
inspect_wheel -> _headers_all -> email.parser.BytesParser -> Message.set_raw.

No complete signed package was produced for that failed larger case. This is
evidence that the required worst-case exercise cannot currently justify 6 GiB
with headroom; it does not claim a completed maximum fixture passed G or that a
larger separately qualified resource profile is impossible. Working set differs
from committed memory; Windows Job Object memory limits govern commit. The
measurement now records both. Fixture directory bytes are not child peak disk.
Maximum positive Linux measurements and production-worker disk accounting remain
unproved. No production default/headroom is accepted.

Negative memory, CPU, descendant and wall tests passed on Windows and Linux after
confirming containment initialization. Windows uses a Job Object. Linux uses
RLIMIT_AS/CPU/CORE/NPROC under an unprivileged identity with capabilities dropped;
root is refused because NPROC would not enforce the contract. The Linux timeout
harness removes the named container, since killing only its Docker client is
insufficient. [Resource evidence](evidence/verifier-resources.json).

Reproduce:

    uv run python scripts/marketplace_verifier_resources.py
    uv run python scripts/marketplace_verifier_resources.py --header-probe --header-mib 64
    uv run python scripts/marketplace_verifier_resources.py --header-probe --header-mib 255
    uv run python scripts/marketplace_verifier_limits_probe.py
    uv run python scripts/marketplace_verifier_limits_probe.py --linux

The 255 MiB probe exits 1 with contained builder exit 2. Increasing a number
without qualification, silently restricting G's format, or silently modifying
its verifier would not meet the brief.

**STAGE I2 IMPLEMENTATION BLOCKED â€” VERIFIER RESOURCE BOUND COULD NOT BE PROVEN**

Suggested review: separately authorize and verify behavior-preserving Stage-G
metadata-parser memory hardening while retaining its format/signature contract;
alternatively qualify a reviewed larger worker resource profile. This request
required the existing verifier unchanged, so that change was not made.

## 15. I2 completion

**Incomplete/unaccepted.** Identity/OIDC, Publisher, membership, ownership,
signing-key and publication services/migrations remain drafts. Draft lint/types
were repaired and OIDC token responses gained a bounded public HTTPX stream.
No accepted Publisher HTTP/browser/CLI integration or verifier parent worker
exists. Upload supervision, current-authority/race tests, lease fencing, audit,
reconciliation and real OIDC/CLI acceptance remain unfinished.

## 16. Hosted migrations

Fresh disposable PostgreSQL 18 upgraded mp0001_catalog_foundation ->
mp0002_publisher_identity -> mp0003_publication_workflow and exercised actual
Artifact writes/immutable version evidence. No developer/deployed DB was migrated.
Historical owner/attribution reconciliation, downgrade guard restoration and full
migration-cycle acceptance remain unfinished. Startup guard remains mp0001.

## 17. I2 acceptance

Versioning probe with evidence output: **exit 0, passed true, empty failure list**.
Focused hosted unit/architecture tests: **113 passed**. Resource qualification
failed. marketplace-publisher-integration is not implemented/passed. I2 is not
accepted.

## 18. I3 implementation

Not started. No local discovery client/transport/API/Web implementation was added.

## 19. I3 security

DNS/TLS/SSRF/privacy acceptance has not been performed for an I3 implementation.
The mandatory secure-transport requirements remain intact for later work.

## 20. I3 acceptance

Not run: I2's prerequisite gate failed. Discovery integration group remains absent.
No passing or skipped assertion is implied.

## 21. Combined E2E

Publisher-to-local-discovery acceptance is unavailable in the partial tree and was
not run. Marketplace E2E group remains absent. No local install/download handoff
was added.

## 22. I1 Marketplace regression

uv run python scripts/check.py marketplace-integration: **exit 1, 50 setup errors**.
Its old cleanup truncates the four I1 tables after upgrading to latest draft
mp0003. upload_operations now references package_releases, so PostgreSQL rejects
that cleanup. The suite was not weakened or pinned back to manufacture green.
Fixtures/assertions and schema guard need adaptation when completing I2. The
separate real mp0003 storage/database/router qualification passed.

## 23. Full check

uv run python scripts/check.py check: **exit 0**. Backend: **3,035 passed,
50 deselected**, 10,484 dependency deprecation warnings, 1,536.47 seconds.
Frontend: **152 passed in 15 files**. Python/frontend lint/types and security scan
also passed. The existing configuration excludes opt-in Marketplace integration
tests. This success does not supersede section 22 or establish I2 acceptance.

## 24. Existing E2E

uv run python scripts/check.py e2e: **exit 0**. Existing supervised local setup,
authentication, scheduling/tool/runtime and Stage-G package journeys ran. This
is separate from the absent Marketplace combined journey.

## 25. Security/static

Ruff check/format, uv lock --check, full Python/frontend static checks, focused
final storage/resource typing and security scan passed. Full check counts are
recorded in section 23. Static and focused checks were repeated for source edits
made while the longer regression run was active. Publisher, discovery and
Marketplace E2E gates remain unrun because prerequisite implementation is blocked.

## 26. Migration invariant

Local, Worker and Scheduler remain 0013_stage_g3_package_registry. No local 0014.
Hosted qualification used mp0003; accepted startup guard remains mp0001. No local
or runtime schema change was introduced.

## 27. Frozen hashes

Final checks matched:

    F: 62cfc0a8039e233c82e2a30ff3fd59495e99395d
    G: 4554ab5e93b35f4bbc4daed164ee5712077c1006
    I: 7bf5cff40eb7ed5defe2d7598b72e0606d29ce7b
    I: 7bf5cff40eb7ed5defe2d7598b72e0606d29ce7b

Frozen ADRs were not edited.

## 28. Scope audit

No I4/H, TUF, Sigstore, local Marketplace artifact download, install ticket,
PackageApplicationService handoff, Marketplace install/update/rebind was added.
No local API/Web/Worker/Scheduler/model/tool/memory behavior changed. Package code
was not imported/executed by these probes.

## 29. Git state

All work remains uncommitted on stage-i at original HEAD. No staging, commit, push,
PR, merge, reset, clean, history rewrite or user-change deletion. .codex/, AGENTS.md,
graphify-out/ and unrelated local files were preserved. Disposable infrastructure
and fixture directories were removed. The pinned Python test image is a local
cache artifact.

### Changed implementation files

- M: apps/cli/pyproject.toml
- M: apps/marketplace/pyproject.toml
- M: apps/marketplace/s3-test-identities.json
- M: apps/marketplace/src/nervos_marketplace_service/application/artifact_reads.py
- M: apps/marketplace/src/nervos_marketplace_service/application/catalog_queries.py
- M: apps/marketplace/src/nervos_marketplace_service/application/ports.py
- M: apps/marketplace/src/nervos_marketplace_service/config.py
- M: apps/marketplace/src/nervos_marketplace_service/infrastructure/catalog_repository.py
- M: apps/marketplace/src/nervos_marketplace_service/infrastructure/models.py
- M: apps/marketplace/src/nervos_marketplace_service/infrastructure/s3_artifact_store.py
- M: apps/marketplace/tests/architecture/test_boundaries.py
- M: apps/marketplace/tests/unit/test_public_api.py
- M: apps/marketplace/tests/unit/test_safety.py
- M: uv.lock
- ??: apps/marketplace/alembic/versions/mp0002_publisher_identity.py
- ??: apps/marketplace/alembic/versions/mp0003_publication_workflow.py
- ??: apps/marketplace/src/nervos_marketplace_service/application/authentication.py
- ??: apps/marketplace/src/nervos_marketplace_service/application/authorization.py
- ??: apps/marketplace/src/nervos_marketplace_service/application/publication.py
- ??: apps/marketplace/src/nervos_marketplace_service/application/publication_ports.py
- ??: apps/marketplace/src/nervos_marketplace_service/application/publisher_management.py
- ??: apps/marketplace/src/nervos_marketplace_service/domain/artifact_reference.py
- ??: apps/marketplace/src/nervos_marketplace_service/domain/identity.py
- ??: apps/marketplace/src/nervos_marketplace_service/infrastructure/oidc.py
- ??: apps/marketplace/src/nervos_marketplace_service/infrastructure/process_limits.py
- ??: apps/marketplace/src/nervos_marketplace_service/infrastructure/s3_final_artifacts.py
- ??: apps/marketplace/src/nervos_marketplace_service/infrastructure/unit_of_work.py
- ??: apps/marketplace/src/nervos_marketplace_service/verifier_child.py
- ??: apps/marketplace/tests/unit/test_versioned_artifacts.py
- ??: docs/stage-i/evidence/storage-versioning.json
- ??: docs/stage-i/evidence/verifier-resources.json
- ??: docs/stage-i/i2-i3-implementation.md
- ??: scripts/marketplace_storage_probe.py
- ??: scripts/marketplace_verifier_limits_probe.py
- ??: scripts/marketplace_verifier_resources.py
- ??: scripts/marketplace_versioning_probe.py

## 30. Stage state

I0/I1 accepted and merged. I2 partial, blocked and unaccepted. I3/I4/I5/H not started.
Implementation status was not marked complete: required criteria did not pass.

## 31. Final result

Reviewed version-pinned storage passed live qualification. Resource proof requires
further review before I2 can progress to acceptance; I3 must wait for green I2.

STAGE I2 + I3 COMBINED IMPLEMENTATION BLOCKED â€” VERIFIER RESOURCE BOUND COULD NOT BE PROVEN
