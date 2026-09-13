"""Unit tests of the min-deps floor resolver (IH-41): the happy path and the
negative paths the CI min-deps job relies on. The script IS a gate - without
these pins a regex or exit-code regression would fail zero tests (IH-41
review finding B)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "min_deps.py"


def run_script(toml_text: str, tmp_path: Path) -> subprocess.CompletedProcess:
    toml_file = tmp_path / "pyproject.toml"
    toml_file.write_text(toml_text, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(toml_file)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_real_pyproject_floors_resolve(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    res = subprocess.run(
        [sys.executable, str(SCRIPT), str(repo / "pyproject.toml")],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.splitlines() == [
        "mcp==2.1.1",
        "paho-mqtt==2.0",
        "pymodbus==3.15.0",
        "pyserial==3.5",
        "pyyaml==6.0.3",
    ]


def test_dependency_without_floor_fails_loudly(tmp_path):
    res = run_script(
        '[project]\nname="x"\nversion="0.1"\ndependencies=["mcp", "pyserial>=3.5"]\n',
        tmp_path,
    )
    assert res.returncode == 2, (res.stdout, res.stderr)
    assert "expected exactly" in res.stderr
    assert "pyserial==3.5" in res.stdout  # the well-formed line still emitted


def test_spec_with_markers_or_ranges_fails_instead_of_mispinning(tmp_path):
    res = run_script(
        '[project]\nname="x"\nversion="0.1"\n'
        'dependencies=["mcp>=1.0,<3", "pyserial>=3.5; sys_platform == \'win32\'"]\n',
        tmp_path,
    )
    assert res.returncode == 2, (res.stdout, res.stderr)
    assert res.stdout == "", "a spec the pin cannot faithfully represent must not be emitted"


def test_missing_file_and_no_deps_fail(tmp_path):
    res = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "absent.toml")],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert res.returncode != 0
    res2 = run_script('[project]\nname="x"\nversion="0.1"\n', tmp_path)
    assert res2.returncode == 2
    assert "no project.dependencies" in res2.stderr
