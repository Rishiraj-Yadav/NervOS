# STAGE G ARCHITECTURE CHANGE REQUEST — BOUNDED WHEEL CORE METADATA

Status: **USER AUTHORIZED / IMPLEMENTED / RESOURCE QUALIFICATION PENDING**.
Recorded 2026-10-02. The user explicitly chose: "Implement the proposed limits,
requalify, then resume I2/I3." ADR 0031 records the amendment. The original
proposal and counterexample below are preserved as the decision history.
Authority: the user's verifier-resource blocker brief, sections 8 and 20,
requires this request before introducing new semantic metadata limits.

## Field requiring review

Repeated `Requires-Dist` values, particularly their environment-marker
expressions. The current G format permits a single value approaching the
256 MiB member limit and millions of repeated declarations. No proposed bound
applies to unused metadata headers or the Description/body.

## Concrete counterexample

The diagnostic builds one requirement with a flat OR expression:

    Requires-Dist: a; extra == 'unselected' or extra == 'unselected' or ...

It is syntactically valid and inactive for both frozen targets. No dependency
wheel is required, no package code is imported and no network is used.

| Marker source | Verifier peak commit | Verifier CPU | Verifier wall |
| --- | ---: | ---: | ---: |
| 4 MiB | 137,711,616 bytes | 8.844 s | 8.843 s |
| 32 MiB | 935,710,720 bytes | 67.844 s | 68.984 s |
| 64 MiB | Final snapshot unavailable | Unavailable | Terminated at 120 s |

The 64 MiB fixture **was accepted and signed by the actual G builder**:
METADATA 67,108,951 bytes; archive 166,980 bytes; archive SHA-256
`4ec796838aa0778537610da1851833c085868483f4e0752e9ce36000feec7490`.
Builder exit 0; peak commit 1,845,403,648 bytes; CPU 253.109 s.
Those builder metrics are not substituted for terminated verifier metrics.

Reproduce with synthetic keys and disposable files:

    uv run python scripts/marketplace_verifier_resources.py --header-probe --header-family large-marker --header-mib 64 --builder-memory-bytes 8589934592 --builder-cpu-seconds 600 --builder-wall-seconds 660 --evidence-file docs/stage-i/evidence/verifier-resources.json

The 8 GiB/600-second settings apply only to fixture generation. Verifier memory
remains 2 GiB and its CPU/wall deadline remains 120 seconds. Increasing builder
limits proves that the failing input is G-accepted; it does not qualify a worker.

## Why the header streaming fix is insufficient

The new header reader discards unused fields/body and retains selected fields
with compat32 interpretation. However, the required public `Requirement` parser
then materializes a marker AST. Header streaming cannot make that parser consume
a marker incrementally. Short-value caches cannot bound arbitrary distinct or
large expressions, and skipping inactive-looking values before validating their
syntax would change accepted behavior.

A general streaming marker grammar/evaluator is technically possible, but it
would be a larger replacement of the existing semantic engine. It requires
separate review and equivalence/resource evidence. The current patch does not
silently introduce such a replacement. Special-casing this repeated OR fixture
would not prove safety for the rest of the permitted grammar.

## Proposed focused policy for review

The smallest directly demonstrated field to restrict is `Requires-Dist`.
A proposed initial budget, to be reviewed and then qualified rather than treated
as an already accepted production guarantee, is:

- At most **64 KiB per decoded logical Requires-Dist occurrence**.
- At most **4 MiB of decoded Requires-Dist values across an entire package**,
  counting agent and dependency wheels and every duplicate occurrence.
- At most **100,000 Requires-Dist occurrences across the package**, so very short
  values cannot create an excessive number of objects/validation operations.

These are proposed safety budgets, not measured final production limits. The
4 MiB single-expression measurement informs the initial aggregate proposal;
all three budget edges, mixed wheelhouses and both supported platforms must be
tested before accepting the policy and selecting memory/time headroom. No claim
is made that these numbers alone finish qualification of Name, Version,
Requires-Python or nested archive pressure.

Apply any approved policy through the shared G validation path so builder and
verifier agree, reject before constructing oversized values/ASTs, and report a
stable explicit validation error. Keep archive identities, signatures, lifecycle,
runtime pins and SDK boundaries unchanged. Update the accepted format contract
through review before enforcement; frozen plans/ADRs remain untouched now.

## Compatibility impact

The proposed policy would reject some packages accepted by current G, including
the valid counterexample above. It is therefore a format acceptance change,
not an invisible parser optimization. Existing signed bytes/digests must never
be rewritten. Review must decide how previously installed packages and new
verification/publication requests interact with any versioned policy. There is
no evidence supporting a claim that no existing external package is affected.

## Alternative preserving the accepted grammar

Separately authorize a general streaming marker parser/evaluator, preserving
syntax/error ordering, compat32, PEP 508 semantics, both environments, duplicates,
and public verifier outputs. Qualify the full accepted worst case under the
same time/resource contract. That route is larger than adding reviewed budgets
and has not been implemented or proven here.

## After the review decision

1. Implement only the approved contract/engine change with differential tests.
2. Complete ignored/relevant/aggregate pressure qualification on Windows and
   unprivileged Linux, including actual disk, CPU/wall and safety headroom.
3. Correct I1's test-owned FK cleanup and pass its real integration gate.
4. Complete and accept I2, then implement and accept I3.
5. Run final repository, existing E2E and combined Marketplace acceptance.

The user subsequently authorized these Requires-Dist budgets. Shared enforcement
and boundary tests are implemented in the unaccepted worktree, with ADR 0031
recording the compatibility impact. No production memory default, frozen-plan
revision or I3 work has been accepted. A separate Version pressure failure was
found during qualification; the user chose streaming normalization to preserve
its acceptance, as recorded in [the Version review](verifier-version-review-request.md).
