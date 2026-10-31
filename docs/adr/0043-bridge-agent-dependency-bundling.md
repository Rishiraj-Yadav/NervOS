# ADR 0043 — Bridge Agent Dependency Bundling

**Status: ACCEPTED — items 1 and 2 implemented; item 3 RESOLVED by "bundle ships
its runtime" (no Worker-side Node runtime).** The investigation below stands as written; the accepted items are the two
smallest changes it identified, in exactly the decided forms:

1. After a process-kind package is materialized, on POSIX only, the executable bit is
   set on the single file named by `runtime.command[0]`. The target must be inside the
   bundle; anything else is refused on every platform. The archive format, signing,
   integrity digests, and every other file's mode are untouched.
2. `process_agent_environment` exports `NERVOS_PACKAGE_PYTHON`, set to the interpreter
   of the package environment (the host's own `sys.executable` inside the sandbox).

Item 3 is RESOLVED differently than first proposed: a Node package ships its own
`node` binary and `node_modules` inside its bundle (as declared assets) and launches
them through `runtime.command` (e.g. `command: ["bin/node", "app.js"]`). That works
because item 1 makes `command[0]` executable at materialization. No Worker-side Node
runtime is built and no new runtime artifact kind is added.

## Question

How does a process-kind package (ADR 0041) ship its own dependencies today — a private
Python `site-packages`, a Node `node_modules`, or a self-contained binary — and what is
the smallest change that would support each shape?

## What exists today (verified, file:line)

### The package archive can already carry arbitrary files

- `packages/nervos-core/src/nervos_core/application/package_builder.py:60-64` — the
  archive layout is `agent.whl`, `manifest.yaml`, `config.schema.json`,
  `dependencies/lock.json`, `dependencies/wheels/*.whl`, `integrity/*`.
- `package_builder.py:99` — `PackageBuildInputs.assets: Mapping[str, Path] | None`:
  arbitrary assets at canonical archive paths are a first-class build input
  (`package_builder.py:91` documents the key convention).
- `packages/nervos-core/src/nervos_core/application/package_storage.py:97-115` —
  `PackageStore.materialize` streams **every** verified member (wheels, lock, and
  assets alike) into the payload root, refusing traversal and overwrite.
- `package_storage.py:128` — the installed bundle lives at
  `packages/<package_id>/<version>/<content_digest>` inside the store.

### What the sandbox mounts

- `apps/worker/src/nervos_worker/package_execution.py:212-218` — for process-kind runs
  the Worker computes that bundle root and passes it as `package_root`.
- `packages/nervos-core/src/nervos_core/infrastructure/sandbox/linux_bubblewrap.py:139-143`
  — the bundle is `--ro-bind`-mounted **read-only**, and for process kind it is also
  the working directory; the scratch directory stays the only writable location.
- `linux_bubblewrap.py:136` — the Python package environment is separately mounted
  read-only (with its `pyvenv.cfg` validated at `linux_bubblewrap.py:72` and
  `sandbox/__init__.py:79-81`).

### Python agents already get an offline site-packages

- `packages/nervos-core/src/nervos_core/application/package_environment.py:161-200` —
  `PackageEnvironmentBuilder.build` creates a private venv
  (`venv.EnvBuilder(with_pip=True)`, line 174) and installs, **offline**
  (`pip install --no-index --no-deps`), the declared dependency wheelhouse plus the
  agent wheel plus the pinned SDK and host wheels. The environment is content-addressed
  by digest and shared across runs.
- A `python`-kind package therefore ships library dependencies today through
  `dependencies/wheels`; a `process`-kind package whose command is a Python program can
  reach the same site-packages **only by invoking the environment interpreter** — the
  child environment (`packages/nervos-package-host/src/nervos_package_host/__main__.py:229-231`,
  built by `process_agent_environment`) does not put the venv on `PATH` or expose a
  `PYTHON` variable.

### Node agents and self-contained binaries

- There is no Node support at all: no `node` runtime artifact, no `node_modules`
  handling, and the manifest's `runtime.command` validation
  (`packages/nervos-core/src/nervos_core/domain/packages.py` — `runtime.command` items)
  only requires relative in-bundle paths. A Node agent that ships its own
  `node_modules` as **assets** would have them materialized and mounted read-only —
  but nothing provides or mounts a `node` interpreter to run it.
- A self-contained single-file binary could be shipped as an asset today, with one gap:
  `package_storage.py:111-115` materializes members with plain `open("xb")`, so the
  POSIX executable bit is **not** preserved from the archive; a `runtime.command` whose
  first item is such a file would fail with `EACCES` on Linux.

## Recommendation (smallest additive change) — items 1 and 2 now implemented

1. **Exec bit**: at materialization, mark process-kind command targets (or every
   payload member under a `bin/` prefix) executable. One flag on the archive entry
   (builder already controls member metadata) plus one `chmod` in
   `PackageStore.materialize`. This is the single unlock for self-contained binaries
   and for `bin/agent`-style wrappers.
2. **Python interpreter for process kind**: export one variable (for example
   `NERVOS_PACKAGE_PYTHON`) pointing at the read-only package environment's
   interpreter in `process_agent_environment`, so a Python foreign agent's command can
   be `["bin/agent.sh"]` (or the command may reference `$NERVOS_PACKAGE_PYTHON`) and
   get the same offline site-packages `python`-kind packages use. No new install step.
3. **Node (RESOLVED by bundling)**: no Worker-side runtime. The bundle carries
   `bin/node` + `node_modules` as assets; `runtime.command: ["bin/node", "app.js"]`
   launches them, with item 1's exec bit making `bin/node` runnable. Validated by
   `packages/nervos-core/tests/unit/test_node_bundle_command.py`.

Items 1 and 2 are additive, manifest-compatible, and need no Worker/core/wire change;
they are implemented as `prepare_process_command_target` (exec bit + containment
refusal, `apps/worker/src/nervos_worker/package_execution.py`, tested in
`apps/worker/tests/unit/test_process_command_target.py`) and the
`NERVOS_PACKAGE_PYTHON` export (`packages/nervos-package-host/src/nervos_package_host/__main__.py`,
`process_agent_environment`, tested in
`packages/nervos-package-host/tests/test_process_agent_environment.py`). Item 3 adds
a new runtime artifact kind, touches installation machinery, and remains deferred.
