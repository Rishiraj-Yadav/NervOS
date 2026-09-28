"""Neutral execution-snapshot domain values for G3 — depends only on primitives."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RunExecutionKind(StrEnum):
    BUILTIN = "builtin"
    PACKAGE = "package"


@dataclass(frozen=True, slots=True)
class RunExecutableSnapshot:
    """Immutable executable identity pinned at Run submission (ADR 0025).

    Deliberately primitive-only: the Run domain depends on this module, which in turn
    depends on no other NervOS domain. Package installation constructs these values;
    it never makes `runs` depend on the package-installation module.
    """

    execution_kind: RunExecutionKind
    installed_package_version_id: int | None
    package_content_digest: str | None
    package_environment_id: int | None
    package_environment_digest: str | None
    package_entrypoint: str | None
    effective_config_json: str
    effective_config_digest: str | None
    agent_instance_config_revision: int | None
    host_protocol_version: str | None
    sdk_api_version: str | None


BUILTIN_EXECUTABLE_SNAPSHOT = RunExecutableSnapshot(
    execution_kind=RunExecutionKind.BUILTIN,
    installed_package_version_id=None,
    package_content_digest=None,
    package_environment_id=None,
    package_environment_digest=None,
    package_entrypoint=None,
    effective_config_json="{}",
    effective_config_digest=None,
    agent_instance_config_revision=None,
    host_protocol_version=None,
    sdk_api_version=None,
)
