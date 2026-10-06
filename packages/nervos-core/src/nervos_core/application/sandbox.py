"""Stage H4 sandbox application contracts (ADR 0035).

The application layer defines the frozen resource policy and the containment port; the
concrete OS adapters live in ``nervos_core.infrastructure.sandbox`` and are composed only
by the Worker. The policy is code-level, deliberately not environment configuration: two
Workers that disagreed about containment would raise it by divergence, the same reasoning
C6 applied to concurrency.

Honesty rules this module enforces by shape:

* :class:`ContainmentResult` states which tier was established; a degraded tier is
  disclosed in Run metadata rather than silently accepted;
* an unavailable platform raises :class:`ContainmentUnavailable` — the Worker refuses to
  run the package instead of running it unsandboxed;
* a macOS (or otherwise unsupported) host can never report success.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

# Frozen resource policy (ADR 0035). Operator settings may only tighten; nothing here is
# runtime-configurable, so a Worker cannot be coaxed into a weaker containment.
MEMORY_LIMIT_BYTES = 512 * 1024 * 1024
MAX_ACTIVE_PROCESSES = 32
MAX_OPEN_FILES = 64
CPU_TIME_SECONDS = 300
FILE_SIZE_LIMIT_BYTES = 64 * 1024 * 1024
MAX_TOTAL_OUTPUT_BYTES = 8 * 1024 * 1024
SCRATCH_DIR_NAME = "scratch"


class ContainmentTier(StrEnum):
    """What the platform adapter actually established, disclosed per Run."""

    FULL = "full"  # Qualified resource, filesystem and network controls together.
    RESOURCE = "resource"  # Resource primitive only; cannot license package execution.
    RLIMITS = "rlimits"  # rlimits only (degraded Linux tier)
    UNSUPPORTED = "unsupported"


class ContainmentUnavailable(Exception):
    """Containment could not be established: the package must not run.

    This is the fail-closed refusal ADR 0035 freezes. The Worker maps it to the safe
    internal-execution outcome before any package code exists; nothing falls back to an
    uncontained process.
    """

    CODE = "sandbox_unavailable"
    MESSAGE = "Package containment is unavailable on this platform; execution is refused."


@dataclass(frozen=True, slots=True)
class ContainmentResult:
    """Evidence that one child process is contained, and how."""

    tier: ContainmentTier
    platform: str
    details: str


@dataclass(frozen=True, slots=True)
class WorkerSandboxCapability:
    """Bounded, public evidence one Worker reports about its package sandbox.

    This is *observed health*, never execution authority: the launch factory still decides
    per package start, and a Worker that loses `bwrap` after registering is refused by the
    factory rather than trusted because a row says otherwise. The three fields are the
    complete vocabulary an operator projection may publish -- no worker id, hostname, path,
    environment or error text crosses this boundary.
    """

    package_execution_supported: bool
    platform: str
    backend: str | None


@dataclass(frozen=True, slots=True)
class SandboxLaunch:
    """A child launch whose restrictions are established before package code starts."""

    command: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str]
    preexec_fn: Callable[[], None] | None = None
    start_new_session: bool = False


class PackageContainment(Protocol):
    """The port a platform adapter implements to contain one package-host process."""

    def prepare(
        self,
        *,
        python: Path,
        scratch: Path,
        environment: Mapping[str, str],
        arguments: tuple[str, ...],
    ) -> SandboxLaunch:
        """Return a protected launch specification before any package code starts."""
        ...

    def establish(self, process_id: int) -> ContainmentResult:
        """Record and verify the already protected child, or raise."""
        ...

    def verify(self, process_id: int) -> None:
        """Re-check containment immediately before dispatch; raise if it cannot be proven."""
        ...

    def terminate(self, process_id: int) -> None:
        """Kill the contained process tree (timeout, cancellation, or refusal to finish)."""
        ...

    def release(self, process_id: int) -> None:
        """Drop containment bookkeeping for a process that has already exited."""
        ...
