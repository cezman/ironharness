"""IH-13 tests: throwaway controller cwd, out-dir marker, safe cleanup."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from ironbench.runner import OUT_MARKER, clean_runs, run_task
from ironbench.tasks import load_task


def make_plant_task(tmp_path, controller: str, name="fake-plant"):
    d = tmp_path / "t"
    d.mkdir(exist_ok=True)
    text = textwrap.dedent(
        f"""
    name: {name}
    description: fake
    entry: solution.py
    target: plant
    timeout_sec: 5
    plant:
      model: heater
      K: 60.0
      T: 10.0
      ambient: 20.0
      setpoint: 50.0
      duration: 60
      dt: 0.5
      requirements:
        steady_error: 2.0
        settle_time: 40.0
        overshoot: 5.0
    expect:
      []
    """
    )
    (d / "task.yaml").write_text(text, encoding="utf-8")
    (d / "solution.py").write_text(controller, encoding="utf-8")
    return load_task(d)


PASSING_CONTROLLER = "GAIN = 0.5\ndef control(t, y, setpoint):\n    return GAIN * (setpoint - y)\n"


def test_plant_controller_cwd_is_the_run_dir(tmp_path):
    # Untrusted controller code gets a throwaway working directory: its cwd is
    # a scratch dir under the run artifacts, not the repo or the task dir.
    task = make_plant_task(
        tmp_path,
        "import os\n"
        "def control(t, y, setpoint):\n"
        "    print('CWD=' + os.getcwd())\n"
        "    return 0.5 * (setpoint - y)\n",
    )
    res = run_task(task, out_dir=tmp_path / "out")
    assert res.passed, (res.error, res.missed)
    # end-to-end: a run marks the out dir, so the dir clean_runs accepts is
    # exactly the dir ironbench runs created
    assert (tmp_path / "out" / OUT_MARKER).is_file()
    logged = [
        ln for ln in res.serial_log.read_text("utf-8").splitlines() if ln.startswith("CWD=")
    ]
    assert logged  # control() runs every dt - many prints, one and the same cwd
    cwds = {Path(ln.split("=", 1)[1]) for ln in logged}
    assert len(cwds) == 1
    cwd = cwds.pop()
    assert cwd.name == "controller-cwd"
    assert cwd.parent.parent == (tmp_path / "out" / "fake-plant")
    assert cwd != task.directory


def test_clean_removes_old_runs_only_in_marked_dirs(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / OUT_MARKER).write_text("ironbench run artifacts\n", encoding="utf-8")

    def make_run(task: str, name: str):
        d = out / task / name
        d.mkdir(parents=True)
        (d / "fake.plant-result.json").write_text("{}", encoding="utf-8")
        return d

    old = make_run("taskA", "run-old")
    import time

    time.sleep(0.02)  # distinct mtimes: clean keeps the newest
    new = make_run("taskA", "run-new")
    solo = make_run("taskB", "run-solo")

    removed = clean_runs(out, keep=1)
    assert removed == 1
    assert not old.exists()
    assert new.exists() and solo.exists()


def test_clean_refuses_dir_without_marker(tmp_path):
    out = tmp_path / "not-ours"
    victim = out / "victim-data"
    victim.mkdir(parents=True)
    with pytest.raises(ValueError):
        clean_runs(out)
    assert victim.is_dir()  # nothing touched
