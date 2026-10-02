# STAGE G ARCHITECTURE CHANGE REQUEST — BOUNDED WHEEL VERSION

Status: user chose streaming PEP 440 normalization, not the proposed Version cap.
Implementation and qualification in progress. Recorded 2026-10-02.

## Exact field and failure

The approved Requires-Dist budgets are implemented and pass their Windows/Linux
positive tests. A separate consumed field, wheel METADATA `Version`, still
amplifies memory through the unchanged PEP 440 `packaging.version.Version`
normalizer. Millions of local-version segments become Python lists, tuples and
strings, even though the caller only needs normalized text.

The synthetic value is `1.0.0+` followed by repeated `a.` and a final `a`.
This is valid PEP 440; agent distribution Version is distinct from the NervOS
manifest's strict SemVer. The fixture uses the real Stage-G builder and signer,
with package code that raises if imported. No code is imported during verification.

| Windows measurement | 16 MiB Version | 64 MiB Version |
| --- | ---: | ---: |
| METADATA bytes | 16,777,279 | 67,108,927 |
| Signed archive bytes | 20,470 | 69,399 |
| Builder exit | 0 | 0 |
| Builder peak commit bytes | 664,686,592 | 2,528,821,248 |
| Builder wall seconds | 7.157 | 34.765 |
| Verifier exit under 2 GiB | 0 | 2, verification_resource_exceeded |
| Verifier peak commit bytes | 661,078,016 | Unavailable in error response |
| Verifier wall seconds | 3.782 | Unavailable in error response |

The builder used a 4 GiB fixture-generation ceiling; that is not a production
verifier ceiling. The 64 MiB signed archive SHA-256 is
`0a6837b8f81359b00ab0f7ad90602129495ea90458f7641210a5cd185278bfa4`.
Machine measurements remain in [resource evidence](evidence/verifier-resources.json).
Do not substitute builder peaks for the failed verifier's unreported peaks.

## Concrete proposed amendment

Authorize **64 KiB per decoded logical wheel METADATA Version value** in the
shared Stage-G builder/verifier inspection path. Reject during streaming before
normalization; preserve the existing exactly-one-field rule, PEP 440 semantics
within the budget, signatures, digests, archive maxima and output bytes.

This is an additional acceptance restriction, separate from ADR 0031's approved
Requires-Dist policy. Previously accepted huge Version values, including this
fixture, would fail fresh build/verification/reinspection. Existing stored bytes
and registry rows would remain unchanged; no grandfathering bypass is proposed.
The proposed value is a practical admission budget, not a measured universal
PEP 440 threshold. Its boundary and both operating systems must be requalified.
No Name, Requires-Python, archive-size or nested-expansion restriction is
authorized by this proposal.

## Alternative preserving all previously accepted values

Implement and differentially qualify a streaming PEP 440 normalization path that
avoids the full Version comparison-key allocation. This requires more work on
normalization, syntax/error equivalence and pathological release/local segments;
it has not been implemented or qualified. A larger memory ceiling alone does
not address the amplification at the existing 256 MiB member maximum.

## Required order after the decision

1. Implement only the selected amendment or normalization work.
2. Rerun Stage-G regressions and the complete final-source resource matrix.
3. Resume I1 cleanup and I2 completion only after resource qualification passes.
4. Pass the I2 gate before beginning I3, then complete all remaining gates.

Frozen F/G/I plans and ADRs 0024–0030 remain unchanged. No Version cap or new
admission restriction is implemented. The user explicitly authorized the
streaming alternative; the implementation uses packaging's public VERSION_PATTERN
for grammar validation and normalizes segments into bounded output chunks.
Packaging Version still normalizes suffixes, and ordinary values keep the
existing path. Differential and resource qualification must pass before resumption.
