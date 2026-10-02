# Stage I4 and I5 implementation and acceptance plan

The frozen authority remains `docs/stage-i/README.md` and ADRs 0027–0030.
This working plan implements the user's I4/I5 brief without changing those files.

1. Reconcile the current I2/I3 worktree with the authoritative delivered status.
   Complete the reachable publisher upload/verification/publication workflow before
   claiming the complete Marketplace journey.
2. Add local migration 0014 for owner-bound exact install tickets. Keep the API,
   Worker and Scheduler schema expectations aligned. Preserve the Stage-G registry,
   installed versions, bindings and historical Run snapshots.
3. Bind configured origin, package ID, exact version, archive/content digests,
   signer, byte size, distribution state and revision. Bound downloads, refuse
   redirects, independently inspect using Stage G and retain the local archive.
4. Separate preparation from explicit installation approval. Show the retained
   verification evidence. Recheck distribution state before handoff and let
   `PackageApplicationService` snapshot, reverify and install the same archive.
5. Preserve manual installation, side-by-side versions, separate instance creation
   and explicit rebind. Display status changes; remote status cannot mutate local
   execution policy. Cover expiry, owner isolation, substitution and retry/crash paths.
6. Prove publish v1 → discover → exact download → verify → approve → install →
   instance → real Worker/package-host execution. Repeat for v2 and explicit rebind,
   preserving v1 history. Stop Marketplace and prove installed execution continues.
7. Run migration, focused regression, frontend, hosted PostgreSQL/S3, security,
   full repository and browser acceptance gates. Update mutable documentation only
   to the verified state; record remaining failures explicitly.

Git finalization follows accepted completion and is a separate user-authorized task.
Stage H remains outside this work.
