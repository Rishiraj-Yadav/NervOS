# ADR 0031: Bounded Requires-Dist admission

Status: user-authorized implementation decision; resource qualification pending.
Date: 2026-10-02.

## Context

The streaming metadata reader removes ignored-field amplification. A genuinely
G-accepted 64 MiB Requires-Dist marker still exceeds the verifier's 120-second
deadline. The user explicitly authorized the proposed limits in response to
[the review request](../stage-i/verifier-metadata-review-request.md).

## Decision

Apply shared G builder/verifier limits to Requires-Dist only: 64 KiB per decoded
logical value (UTF-8 encoded size), 4 MiB of values across agent/dependency wheels,
and 100,000 occurrences across the package. Count duplicates and inactive markers.
Reject oversized individual values while streaming before Requirement parsing.
Retain existing syntax, markers, target environments, duplicate semantics and
outputs within those budgets. Do not cap Description/body or unused headers.

The package-wide budget is scoped to one inspection pass, not accumulated across
reinspection of the same archive. Standalone wheel/closure APIs also enforce
the policy. No production configuration can disable or raise these maxima.

## Compatibility and boundaries

This intentionally narrows current acceptance: large previously accepted
requirements may fail a new build, verification or reinspection. Existing signed
bytes/digests and registry records are not rewritten or removed. Installed
artifacts requiring fresh wheel inspection can now fail that inspection; no
grandfathering bypass is introduced. Ordinary signed output remains unchanged.
This policy changes no runtime authority, secrets, model/tool/memory contracts,
package execution pinning, schema or Marketplace trust rules.

Frozen predecessor documents remain unchanged. This ADR records the explicitly
authorized amendment. Do not mark G hardening/I2 complete until the resource
matrix and all relevant acceptance gates pass.

## Consequences

Validate individual/aggregate/count boundaries, mixed wheels, duplicate/inactive
cases, early rejection and unchanged ignored metadata. Qualify Windows/Linux
memory, disk and CPU/wall with headroom. Budgets alone are not proof that all
other accepted fields or nested archives fit the selected production profile.
