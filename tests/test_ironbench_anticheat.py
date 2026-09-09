"""Anti-cheat tests (IH-14): a fake "solution" that dumps the expected strings
must FAIL on every text task, and an open-loop constant controller must FAIL
on every plant task (the tasks carry mid-run disturbances for exactly this).

The verbatim cheater models an adversary with full knowledge of task.yaml: it
prints the literal expect patterns up front. It is defeated by the runner's
wait-serial anchor (an answer printed before the stimulus asked for it is a
failed run) and by regex expectations that do not match their own source.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from ironbench.runner import run_task
from ironbench.tasks import load_tasks

TASKS_DIR = Path(__file__).parents[1] / "src" / "ironbench" / "tasks"

ALL_TASKS = sorted(load_tasks(TASKS_DIR), key=lambda t: t.name)
UNIX_TASKS = [t for t in ALL_TASKS if t.target == "unix"]
PLANT_TASKS = [t for t in ALL_TASKS if t.target == "plant"]


def run_with_entry(task, code: str, tag: str, tmp_path: Path):
    work = tmp_path / tag / "work"
    work.mkdir(parents=True)
    (work / "solution.py").write_text(code, encoding="utf-8")
    cheated = dataclasses.replace(task, directory=work, entry="solution.py")
    return run_task(cheated, out_dir=tmp_path / tag / "out")


def verbatim_source(task) -> str:
    """A cheater that prints the literal expect patterns (full yaml knowledge)."""
    if not task.expect:
        return "print('nothing to fake')\n"
    joined = ", ".join(repr(p) for p in task.expect)
    return f"for _s in ({joined}):\n    print(_s)\n"


@pytest.mark.parametrize("task", UNIX_TASKS, ids=lambda t: t.name)
def test_unix_verbatim_cheater_fails(task, tmp_path):
    res = run_with_entry(task, verbatim_source(task), "cheat", tmp_path)
    assert not res.passed, (
        f"{task.name} is verbatim-cheatable (printed the expect strings and passed): "
        f"error={res.error!r}"
    )


@pytest.mark.parametrize("task", PLANT_TASKS, ids=lambda t: t.name)
def test_plant_open_loop_constant_fails(task, tmp_path):
    # A constant power tuned to the (hidden) plant parameters holds the setpoint
    # before the mid-run disturbance and loses it after - the tasks carry
    # disturbances precisely to fail this open-loop cheat.
    u = (task.plant["setpoint"] - task.plant["ambient"]) / task.plant["K"]
    res = run_with_entry(task, f"def control(t, y, setpoint):\n    return {u!r}\n", "cheat", tmp_path)
    assert not res.passed, (
        f"{task.name} is open-loop-cheatable (constant u={u:.3f} passed): "
        f"error={res.error!r}"
    )
