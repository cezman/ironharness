"""Shared test fixtures (no test files here)."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest


def _unix_wsl_ready() -> bool:
    """The golden unix-target runs need more than the wsl.exe binary: a
    configured distro with the micropython binary in place. GitHub windows
    runners ship wsl.exe without any distro - probing `wsl -d <distro> test -x`
    fails there fast, so the golden tests skip instead of erroring."""
    if shutil.which("wsl") is None:
        return False
    from ironbench.runner import _wsl_distro

    binary = os.environ.get("IRONBENCH_UNIX_BIN", "~/bin/micropython")
    probe = subprocess.run(
        ["wsl", "-d", _wsl_distro(), "--", "bash", "-c", f"test -x {binary}"],
        capture_output=True,
        timeout=60,
        check=False,
    )
    return probe.returncode == 0


@pytest.fixture(scope="session")
def wsl_unix_ready() -> bool:
    return _unix_wsl_ready()
