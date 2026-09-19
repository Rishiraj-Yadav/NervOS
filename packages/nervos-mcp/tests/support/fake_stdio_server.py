"""The stdio entry point for the fake MCP server.

The integration test launches this as a real child process with ``sys.executable`` and no shell:

    python -m tests.support.fake_stdio_server

It is invoked as a module (not a bare path) so the relative import below is the same one the type
checker sees, and so the child starts the exact server module the in-process HTTP variant uses. The
ledger's file path, when set, arrives through ``NERVOS_FAKE_MCP_LEDGER_PATH``.
"""

from __future__ import annotations

from .fake_mcp_server import build_server


def main() -> None:
    """Serve the fake server over stdio until the client closes the session."""
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
