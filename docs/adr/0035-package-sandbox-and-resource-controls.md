# ADR 0035 — Package Sandbox and Resource Controls

Status: Accepted for Stage H implementation (H4)

## Context

ADR 0026 froze the honest non-sandbox guarantee: the Stage-G subprocess boundary is dependency
and architectural isolation, **not hostile-code containment** — a malicious package running as
the same OS user can read files and the network. Stage H owns real containment. The unit-test
honesty rule stands: a mocked test proves nothing about an operating system.

## Decision

### What isolation actually is, per platform

| Platform | Primitive | Coverage |
|---|---|---|
| Windows 10/11 (test host) | Job Object with `JOB_OBJECT_LIMIT_PROCESS_MEMORY`, `JOB_OBJECT_LIMIT_JOB_TIME`, active-process limit, `JOB_OBJECT_UILIMIT_*` handles/UI restrictions | memory, CPU-time, process count, UI/handle interference |
| Linux 6.x | cgroup v2 (memory, pids, cpu.max) where writable + `resource.setrlimit` (AS, NOFILE, NPROC, CPU, FSIZE) + `start_new_session` | memory, pids, CPU, fds, file size |
| Linux (no cgroup write access) | rlimits only | degraded mode, disclosed at launch |
| macOS | none adequate in-process | **packages refuse to execute** |

Containment is established by the Worker **before** the host handshake; the containment adapter
is a Worker-owned infrastructure module over OS primitives (ctypes on Windows; stdlib `resource`
/ cgroup files on Linux). No Docker/Kubernetes/Redis/Kafka; no new service.

### Policy applied to every package process

- **Memory:** hard cap (default 512 MiB, operator-tightening only). Exceeded → process killed
  → attempt fails with the safe internal-execution error, never replayed (ambiguous per 0017).
- **CPU time / wall clock:** bounded by the existing Attempt tool/loop budgets plus an outer
  containment watchdog; exceeding either kills the process tree.
- **Processes/threads:** active-process limit (default 32). Child spawning is *possible* within
  the Job Object/cgroup but every child shares the same containment — the escape vector is the
  limit itself, not per-child policy.
- **File descriptors:** `NOFILE` rlimit (Linux) / handlequoting UI restriction (Windows).
- **Output:** the existing bounded wire protocol (`MAX_FRAME_BYTES` 1 MiB, bounded run_result)
  plus a total-stdout/bytes cap; a host exceeding it is killed.
- **Temporary storage:** `FSIZE` rlimit (Linux) and a Worker-created per-run scratch directory
  under the package environment that is the process's CWD; quota enforced by the rlimit.
- **Filesystem:** the process runs with CWD = its scratch directory and the minimal environment
  already built by `package_host_environment()`. On POSIX, the store and database directories
  are protected by OS permissions (the DB gains restrictive file mode). **Windows cannot deny
  specific paths to a same-user child**; the honest, stated boundary is: filesystem *read*
  protection on Windows is partial, and the threat model row documents it. Traversal out of the
  scratch CWD is still bounded by the same-user permission model, not claimed as containment.
- **Network:** packages get **no sockets**. The package-host protocol has no network message and
  gains none; model/tool/connector calls are mediated (Stage D) and brokered (H2). The host
  process's network denial is enforced structurally (no HTTP client exists in the host package,
  and POSIX rlimit/namespace-based denial where available). Any direct outbound request must
  fail; the mediated path is the only egress.

### Fail-closed when containment is unavailable

The Worker launches a package only when its platform adapter reports containment established.
Unsupported platform, failed primitive setup, or a containment verification error →
`ModelProviderError(INTERNAL_EXECUTION_ERROR)` **before any package code runs**, with a Run
Event recording the static reason. There is no "run anyway" flag, no operator bypass, no
degraded silent mode. (Linux rlimits-only *is* an established containment tier, disclosed in
Run metadata — it is not the unavailable case.)

### Worker authority and IPC

The containment host is the Worker's child; the Worker remains the sole execution authority
(claim, lease, retry, cancel, terminalize). The wire protocol is unchanged in authority: a
package cannot request a host action outside the existing message set (`model_request`,
`tool_request`, `run_result`, `log_event`, `cancel` acknowledgement). The containment adapter
adds **no** message type. `log_event` volume is bounded and dropped above the cap.

### Testing honesty

- **Windows:** real Job Object tests in the local suite (this CI host is Windows 11): memory-cap
  kill, process-count kill, UI-restriction sanity, traversal/protected-path attempts, and
  network-failure probes.
- **Linux:** rlimit tier tests run where the suite executes; cgroup-tier tests are marked and
  skipped with an explicit reason when cgroup v2 is not writable — reported as *pending*, never
  as passing.
- **macOS:** an explicit test asserts refusal-to-run.
- Mocked unit tests cover policy plumbing only and are documented as such; they never appear as
  OS-isolation evidence.

## Consequences

- Running untrusted packages becomes defensible on Windows and Linux; macOS packages are
  intentionally unusable rather than unsandboxed — stated in docs and UI.
- Same-user local malware remains out of scope; containment protects *the package from the
  machine's secrets* only to the extent OS primitives allow on each platform, and the residual
  Windows filesystem-read gap is disclosed, not hidden.
- The Stage-G install/health path is unchanged; containment applies at execution time.
