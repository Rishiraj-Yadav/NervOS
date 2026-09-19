"""Operator configuration: a user names a server or an alias; only the operator names a mechanism.

These tests pin the refusals that keep a database row from choosing a binary, choosing a working
directory, or spending a credential somewhere the operator never bound it -- and they pin that the
secret itself never appears in a formatted representation.
"""

from __future__ import annotations

import sys

import pytest
from nervos_mcp.errors import McpConfigurationError, McpErrorCode
from nervos_mcp.operator_config import (
    CredentialAliasSpec,
    McpOperatorConfig,
    SecretValue,
    StdioServerSpec,
)

SERVER_KEY = "docs"
ALIAS = "docs-token"
ENV_VAR = "NERVOS_TEST_DOCS_TOKEN"
TARGET = "https://docs.example/mcp"
SECRET = "SYNTHETIC-DOCS-TOKEN-DO-NOT-LEAK"


def _stdio_spec() -> StdioServerSpec:
    return StdioServerSpec(
        server_key=SERVER_KEY, executable=sys.executable, args=("-m", "docs_server")
    )


def _operator() -> McpOperatorConfig:
    return McpOperatorConfig(
        stdio_servers={SERVER_KEY: _stdio_spec()},
        credential_aliases={
            ALIAS: CredentialAliasSpec(
                alias=ALIAS,
                env_var=ENV_VAR,
                allowed_targets=frozenset({TARGET}),
            )
        },
    )


def test_a_stdio_spec_requires_an_absolute_executable() -> None:
    with pytest.raises(ValueError):
        StdioServerSpec(server_key=SERVER_KEY, executable="docs_server")


def test_a_stdio_spec_requires_an_ascii_server_key() -> None:
    with pytest.raises(ValueError):
        StdioServerSpec(server_key="döcs", executable=sys.executable)


def test_a_declared_stdio_server_is_returned_by_key() -> None:
    spec = _operator().stdio_server(SERVER_KEY)

    assert spec.server_key == SERVER_KEY
    assert spec.executable == sys.executable


def test_an_undeclared_server_key_is_refused() -> None:
    with pytest.raises(McpConfigurationError) as failure:
        _operator().stdio_server("ghost")

    assert failure.value.code is McpErrorCode.SERVER_KEY_UNKNOWN


def test_an_unknown_credential_alias_is_refused() -> None:
    with pytest.raises(McpConfigurationError) as failure:
        _operator().check_alias_target("ghost", TARGET)

    assert failure.value.code is McpErrorCode.CREDENTIAL_ALIAS_NOT_AVAILABLE


def test_an_alias_presented_to_an_unbound_target_is_refused() -> None:
    with pytest.raises(McpConfigurationError) as failure:
        _operator().check_alias_target(ALIAS, "https://other.example/mcp")

    assert failure.value.code is McpErrorCode.CREDENTIAL_ALIAS_NOT_AVAILABLE


def test_an_alias_bound_to_the_target_is_returned() -> None:
    spec = _operator().check_alias_target(ALIAS, TARGET)

    assert spec.env_var == ENV_VAR


def test_resolve_secret_reads_the_variable_the_alias_names() -> None:
    value = _operator().resolve_secret(ALIAS, TARGET, environ={ENV_VAR: SECRET})

    assert isinstance(value, SecretValue)
    assert value.reveal() == SECRET


def test_a_secret_value_never_formats_its_text() -> None:
    value = _operator().resolve_secret(ALIAS, TARGET, environ={ENV_VAR: SECRET})

    assert SECRET not in repr(value)
    assert SECRET not in str(value)


def test_resolve_secret_refuses_a_missing_variable() -> None:
    with pytest.raises(McpConfigurationError) as failure:
        _operator().resolve_secret(ALIAS, TARGET, environ={})

    assert failure.value.code is McpErrorCode.CREDENTIAL_ALIAS_NOT_AVAILABLE


def test_resolve_secret_refuses_a_blank_variable() -> None:
    with pytest.raises(McpConfigurationError) as failure:
        _operator().resolve_secret(ALIAS, TARGET, environ={ENV_VAR: "   "})

    assert failure.value.code is McpErrorCode.CREDENTIAL_ALIAS_NOT_AVAILABLE


def test_resolve_secret_reproves_the_alias_binding() -> None:
    with pytest.raises(McpConfigurationError) as failure:
        _operator().resolve_secret(ALIAS, "https://other.example/mcp", environ={ENV_VAR: SECRET})

    assert failure.value.code is McpErrorCode.CREDENTIAL_ALIAS_NOT_AVAILABLE
