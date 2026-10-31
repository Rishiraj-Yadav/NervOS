# ADR 0043 item 1: the process-kind command target becomes executable, in-bundle only.

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from nervos_core.application.model_completion import (
    INTERNAL_EXECUTION_ERROR,
    ModelProviderError,
)
from nervos_worker.package_execution import prepare_process_command_target


def _bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    (bundle / "bin").mkdir(parents=True)
    (bundle / "bin" / "agent").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    return bundle


@pytest.mark.skipif(
    os.name != "posix",
    reason="executable bits are a POSIX concern (ADR 0043 item 1)",
)
def test_command_target_gets_the_executable_bit_posix(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    target = bundle / "bin" / "agent"
    target.chmod(0o644)

    prepare_process_command_target(bundle, ["bin/agent", "--flag"])

    assert target.stat().st_mode & stat.S_IXUSR
    assert target.stat().st_mode & stat.S_IXGRP
    assert target.stat().st_mode & stat.S_IXOTH
    # Only the mode changed; the content is untouched.
    assert target.read_text(encoding="utf-8") == "#!/bin/sh\nexit 0\n"


def test_a_target_outside_the_bundle_is_refused_everywhere(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    outside = tmp_path / "outside.sh"
    outside.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    with pytest.raises(ModelProviderError) as error:
        prepare_process_command_target(bundle, ["../outside.sh"])
    assert error.value.code == INTERNAL_EXECUTION_ERROR

    with pytest.raises(ModelProviderError):
        prepare_process_command_target(bundle, [str(outside)])


def test_a_missing_or_empty_command_target_is_refused(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    with pytest.raises(ModelProviderError):
        prepare_process_command_target(bundle, [])
    with pytest.raises(ModelProviderError):
        prepare_process_command_target(bundle, ["bin/does-not-exist"])
