# ADR 0044 + ADR 0043 item 2: the child environment points every vendor SDK at the
# loopback shim and names the package environment's interpreter.

from __future__ import annotations

import sys

from nervos_package_host.__main__ import process_agent_environment


def test_process_agent_environment_points_every_sdk_at_the_shim() -> None:
    env = process_agent_environment("http://127.0.0.1:45123", "run-key")

    assert env["OPENAI_BASE_URL"] == "http://127.0.0.1:45123/v1"
    assert env["OPENAI_API_KEY"] == "run-key"
    # Anthropic's SDK appends /v1 itself, so it gets the bare origin.
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:45123"
    assert env["ANTHROPIC_API_KEY"] == "run-key"
    # Google SDKs append /v1beta/... themselves and read either key variable.
    assert env["GOOGLE_GEMINI_BASE_URL"] == "http://127.0.0.1:45123"
    assert env["GEMINI_API_KEY"] == "run-key"
    assert env["GOOGLE_API_KEY"] == "run-key"
    assert env["NERVOS_MCP_URL"] == "http://127.0.0.1:45123/mcp"
    # ADR 0043 item 2: the package environment's interpreter, so a Python foreign
    # agent uses the same offline site-packages as python-kind packages.
    assert env["NERVOS_PACKAGE_PYTHON"] == sys.executable
    assert len(env) == 9
