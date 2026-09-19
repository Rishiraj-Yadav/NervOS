"""The stdio transport: an operator-declared process, started without a shell.

Everything about *which* process runs was decided in :mod:`nervos_mcp.operator_config`. This module
only turns that decision into a launch, and it adds three properties the SDK's own helper does not
carry on its own:

* the child environment is the operator's declared additions plus the credential material for that
  server and nothing else -- the SDK already refuses to inherit the whole process environment, so
  what is left to get right is not *adding* anything;
* the child's stderr is discarded rather than inherited or logged. Raw stderr is
  server-controlled text: it may contain the credential, and section 13 of the D5 authorization is
  explicit that it must never be recorded. Discarding it (an explicit permission there, and the
  only by-construction option -- the SDK hands this stream straight to the operating system, so a
  pipe we do not drain would fill and deadlock the child, and a file would grow without bound)
  removes the deadlock risk, the growth risk, and the leak in one step;
* the launch is `create_subprocess_exec`-shaped from end to end. There is no shell, no `cmd /c`, no
  `sh -c`, and no string concatenation that could become one.

Teardown is the SDK's: `stdio_client` closes stdin, waits a bounded grace period, then kills the
process tree, which is the platform-safe shape D5 requires. This module does not add a second,
competing teardown.
"""

from __future__ import annotations

import os
from typing import TextIO

from mcp import StdioServerParameters
from mcp.client import Transport
from mcp.client.stdio import stdio_client

from nervos_mcp.operator_config import SecretValue, StdioServerSpec


def _discarding_errlog() -> TextIO:
    """A real, empty sink for the child's stderr.

    It must be a genuine open file: the SDK passes this object to the OS as the child's stderr, so
    an in-memory buffer would be rejected outright. ``os.devnull`` accepts any amount of output
    without ever blocking the writer and without retaining a byte of it.
    """
    return open(os.devnull, "w", encoding="utf-8")


def build_child_environment(
    spec: StdioServerSpec,
    *,
    credential: tuple[str, SecretValue] | None = None,
) -> dict[str, str]:
    """Build the child's environment: the operator's static additions, plus one credential.

    The SDK merges this over its own platform allowlist (`PATH`, `TEMP`, `SYSTEMROOT` and the like),
    so the result is never the Worker's whole environment. ``credential`` is the alias's variable
    *name* paired with its value, resolved moments earlier; it is the only secret that can appear
    here, and only because this server's declaration named the alias.
    """
    env: dict[str, str] = dict(spec.env)
    if credential is not None:
        name, value = credential
        env[name] = value.reveal()
    return env


def stdio_transport(
    spec: StdioServerSpec,
    *,
    credential: tuple[str, SecretValue] | None = None,
) -> Transport:
    """Build the stdio transport for one operator-declared server.

    ``command`` and ``args`` come from the declaration and are passed as separate values, which is
    what keeps an argument containing a space or a quote from becoming shell syntax.
    """
    params = StdioServerParameters(
        command=spec.executable,
        args=list(spec.args),
        env=build_child_environment(spec, credential=credential),
        cwd=spec.working_dir,
    )
    errlog = _discarding_errlog()
    transport: Transport = stdio_client(  # pyright: ignore[reportArgumentType]
        params, errlog=errlog
    )
    return transport


__all__ = ["build_child_environment", "stdio_transport"]
