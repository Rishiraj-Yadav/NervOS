# NervOS Documentation Rules

Documentation is part of the deliverable, not an afterthought.

## Source of truth

`docs/implementation-status.md` is the source of truth for verified state.

Read it before starting major work.

Update it after an accepted milestone, and only after that milestone's
acceptance criteria actually pass.

Do not mark a milestone complete while its criteria are unverified.

## Current vs future

Never describe future or target architecture as implemented.

Clearly separate what Stage A implements today from what is planned later.

When a milestone completes, update the tense of descriptions that have become
current; do not leave stale "will" statements about delivered behavior.

## Behavior changes

Update behavior documentation in the same change as the behavior change.

A change that alters public behavior is not complete until its documentation
matches.

## Architecture decisions

Record significant architecture decisions as ADRs under `docs/adr/`.

An ADR is required when a change alters an architecture invariant or boundary,
not for routine implementation detail.

## Secrets

Never write credentials, tokens, or secret values into documentation.

`.env.example` may contain only fake placeholder values.

Do not copy real values from `.env`, logs, or configuration into any document.

## Review

Documentation is reviewed before a milestone is declared complete.

Review accuracy, not only existence: confirm statements match the verified
state recorded in `docs/implementation-status.md`.
