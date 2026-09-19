"""Tests for the ``nervos-mcp`` package.

The package is a deliberate import boundary: nothing here reaches into an SDK that production code
does not already import, and every test below runs against injected doubles, a loopback-only fake
server, or a real temporary file. No test touches a developer database or the public Internet.
"""
