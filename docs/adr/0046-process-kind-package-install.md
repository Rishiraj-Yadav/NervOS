# ADR 0046 — Process-Kind Package Install

**Status: ACCEPTED** — Implemented per the Owner decisions below. Closes the Phase A2
stop recorded in the bridge plan and `docs/implementation-status.md`.

## Context

ADR 0041 added `runtime.kind: "process"` packages that run an arbitrary command through
the loopback shim. The real install path, however, was built around a Python
`module:object` entrypoint: `SqlPackageRegistryPersistence.begin_install` splits
`runtime.entrypoint` into two NOT NULL columns, and the run-executable snapshot mapper
rebuilds `package_entrypoint` from them. A process manifest has no entrypoint, so
install raised `ValueError` before the adapter ever ran — the A2 stop.

The database change is authorized for this task only (migration `0025`). Owner
decisions: (a) `runtime_kind` on `installed_package_versions`; (b) nullable entrypoint
columns with a kind/entrypoint pairing CHECK; (c) for the job/execution record's
`package_entrypoint`, store the stable non-secret label `process:<command[0]>` in the
EXISTING column with no schema change there.

## Task 1 — Trace (read before changing anything)

| # | Place | file:line (re-measured) | Breaks for process kind? | Fixed how |
|---|---|---|---|---|
| 1 | Install insert splits `runtime.entrypoint` | `packages/nervos-core/src/nervos_core/infrastructure/database/packages.py:133` (`begin_install`) | **yes** — `ValueError: not enough values to unpack` | Branch on `runtime.kind`: python splits as before; process stores `runtime_kind='process'`, NULL entrypoint columns |
| 2 | `InstalledPackageVersionRecord` entrypoint columns NOT NULL | `packages/nervos-core/src/nervos_core/infrastructure/database/models.py:310-311` | **yes** — NULL insert violates NOT NULL | Migration 0025 makes them nullable, adds `runtime_kind` + pairing CHECKs (model mirrors) |
| 3 | Run-executable snapshot mapper rebuilds `package_entrypoint` | `packages/nervos-core/src/nervos_core/infrastructure/database/packages.py:1288-1297` | **yes** — `str(None)` | For `runtime_kind='process'`, build the label `process:<command[0]>` (command from the stored `manifest_bytes`; python rows unchanged) |
| 4 | Package detail mapper | `packages/nervos-core/src/nervos_core/infrastructure/database/packages.py:1380-1381` | **yes** — `str(None)` | Optional fields + `runtime_kind` + `command` from the manifest |
| 5 | `InspectedPackageMetadata` / `PackageVersionDetail` domain types | `packages/nervos-core/src/nervos_core/domain/package_query.py:61-62,106-107` | **yes** — required `str` | Fields become `str | None`; add `runtime_kind` and `command` |
| 6 | `PackageQueryService.inspect_artifact` splits entrypoint | `packages/nervos-core/src/nervos_core/application/package_query.py:100-101` | **yes** — `""` split | Branch on `manifest.runtime.kind` |
| 7 | Install health check imports the entrypoint | `packages/nervos-core/src/nervos_core/application/package_installation.py:258-266` | **yes** — entrypoint is `""` | Health check runs for python kind only; a process package has no Python entrypoint to import (ADR 0043 item 2 makes the interpreter available to the child instead) |
| 8 | Worker `package_entrypoint is None` refusal | `apps/worker/src/nervos_worker/package_execution.py:141` | **no** — with the label, package rows always carry a non-None `package_entrypoint`; the Worker already reads `runtime.kind`/`runtime.command` from the manifest (`:157-159`) | None (verified by the A2 journey) |
| 9 | Definition-resolver chain | `packages/nervos-core/src/nervos_core/infrastructure/database/packages.py:1196-1232` (`SqlInstalledPackageDefinitionSource`) and `packages/nervos-core/src/nervos_core/application/package_manifest.py:223` (`project_agent_definition`) | **no** — the resolver reads stored `manifest_bytes` and projects identity/display/limits only; it never touches the entrypoint columns | None (the earlier "resolver chain" suspicion did not survive tracing) |
| 10 | API schemas | `apps/api/src/nervos_api/api/schemas.py:817-818,856-857` | yes — required `str` | Optional + `runtime_kind` + `command` |
| 11 | API routes | `apps/api/src/nervos_api/api/routes/packages.py:59-60,179-180` | yes — pass-through | Pass the new fields through |
| 12 | CLI inspect output | `apps/cli/src/nervos_cli/main.py:71` | yes — prints `None:None` | Print kind + command for process, unchanged line for python |
| 13 | Job/execution record CHECK (`package_entrypoint NOT NULL` for `execution_kind='package'`) | `packages/nervos-core/src/nervos_core/infrastructure/database/models.py:451`, `apps/api/alembic/versions/0013_stage_g3_package_registry.py:66-75` | **no** — decision (c) stores the label in the existing column, satisfying the existing CHECK; no schema change there | None |
| 14 | Schema-revision pins | `apps/worker/src/nervos_worker/app.py:95`, `apps/scheduler/src/nervos_scheduler/app.py:41`, `apps/api/tests/integration/test_migrations.py:1571,1595,1635`, `apps/marketplace/tests/architecture/test_boundaries.py:78` | yes — head moves to `0025` | Pins updated |

## Decision

Migration `0025_process_kind_packages` (SQLite batch rebuild, `recreate="always"`, the
repo's established pattern from 0021/0023):

- add `runtime_kind VARCHAR(8) NOT NULL DEFAULT 'python'` with `CHECK (runtime_kind IN
  ('python','process'))`;
- make `entrypoint_module`/`entrypoint_object` nullable, with a named CHECK enforcing
  the pairing: python requires both NOT NULL, process requires both NULL. Existing rows
  become `runtime_kind='python'` with their entrypoints intact — python-kind rules are
  exactly as strict as before, and no CHECK is weakened;
- downgrade **refuses** (raises, aborting the migration) if any
  `runtime_kind='process'` row exists; otherwise restores the previous shape exactly
  (columns NOT NULL again, `runtime_kind` and both new CHECKs dropped).

Design (c) verdict: the label approach works — `package_entrypoint` is already a
nullable `VARCHAR(512)` on the jobs record and the `execution_kind='package'` CHECK
only demands non-NULL, which the label satisfies — so **no schema change** to the
jobs table and no CHECK change there. `RunExecutableSnapshot.package_entrypoint`
stays `str | None` in the domain (`domain/execution.py:28`).

Python-kind behavior is unchanged everywhere: same split, same stored values, same
`module:object` API shape, same health check, same Worker initialize frame.

## Consequences

- Process-kind packages install, bind, and run through the real persistence, adapter,
  mediator and audit paths; the A2 journey test is un-skipped.
- API/CLI consumers see `runtime_kind` and `command` (process) alongside the optional
  entrypoint fields (python responses unchanged in shape and values).
- Existing databases must run `uv run alembic -c apps/api/alembic.ini upgrade head`
  (worker and scheduler refuse to start against the old revision).

## Verification

- Migration tests: upgrade from 0024 keeps python rows valid; CHECKs accept valid
  python/process rows and reject invalid mixes; downgrade refuses with process rows and
  restores the old shape otherwise (`apps/api/tests/integration/test_migrations.py`).
- Persistence round-trips for both kinds (`packages/nervos-core/tests/unit/
  test_package_registry_persistence.py` additions); API route tests keep the python
  shape unchanged and add a process case; the A2 journey
  (`apps/worker/tests/integration/test_real_worker_process_package.py`) is un-skipped
  and asserts mediation, audit, denial, and credential isolation.
