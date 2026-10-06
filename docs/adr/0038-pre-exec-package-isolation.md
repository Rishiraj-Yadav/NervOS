# ADR 0038 — Pre-exec package isolation and qualified Linux launcher

Status: Accepted by explicit approval of the runtime-qualification implementation plan on
2026-10-04.

## Context

ADR 0037 correctly requires filesystem and network denial before arbitrary package code
starts. The original `PackageContainment.establish(process_id)` contract attached resource
controls after process creation. Windows Job Objects and POSIX rlimits bound resources but
could not stop an already-started package from reading same-user files or opening the network.
They therefore cannot authorize production package execution.

## Decision

- The containment port prepares the complete child command, environment and pre-exec policy
  before process creation. Post-spawn establishment only records and verifies a child already
  launched through that policy.
- Linux is the first supported package-execution platform. It requires `bubblewrap` (`bwrap`)
  and uses user, mount, PID and network namespaces, capability removal and frozen POSIX rlimits.
- The sandbox exposes read-only system/runtime roots and the exact immutable package environment,
  plus one private writable attempt scratch directory. It does not mount the NervOS database,
  user home, configuration, secret-key files, other package stores or other attempts' scratch.
- A new network namespace provides no external interface. Model, MCP and account operations remain
  Worker-mediated over the existing package-host standard streams. No credential enters the child.
- The same protected launch contract applies to package import/health checks. Installation must
  fail closed if protection cannot be prepared.
- Windows Job Objects and plain POSIX rlimits remain resource-test primitives. Windows and macOS
  production package execution continue to refuse with `sandbox_unavailable`.
- Docker is permitted only as a deterministic Linux qualification environment. It is not a
  production runtime dependency or a new NervOS execution path.

## Failure and lifecycle behavior

Missing `bwrap`, namespace refusal, mount failure, handshake failure or verification failure causes
a static `sandbox_unavailable`/internal execution outcome before a package request is dispatched.
Cancellation, timeout, lease loss and protocol failure terminate the sandbox process group. Scratch
is removed in the existing attempt cleanup path. Existing output and frame bounds remain enforced.

## Compatibility

Run → Job → Attempt → Worker authority, Stage-D mediation, Stage-F immutable snapshots, Stage-G
package pinning and Stage-H account/approval fencing do not change. The wire protocol gains no
message. Test-only injection may still use resource primitives, but such tests are never reported
as filesystem/network qualification.

## Acceptance

An enabled platform needs real-kernel probes proving a protected host file cannot be read, direct
network connection fails, scratch remains writable, descendants are terminated and a normal signed
package still completes through mediated model/tool IPC. Other platforms remain explicitly refused.
