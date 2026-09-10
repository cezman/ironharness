"""Golden debug-class tasks (IH-7): the shipped buggy firmware must FAIL its
own task (the planted bug is detectable), and the buggy listing must reach the
agent - the solve loop never reads files, so the description's embedded code
block is the agent's only input and must carry buggy.py verbatim. The fixed
goldens are pinned live (WSL micropython) by the anti-cheat suite's golden
regression (GOLDEN_UNIX)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from ironbench.runner import run_task
from ironbench.tasks import load_tasks

TASKS_DIR = Path(__file__).parents[1] / "src" / "ironbench" / "tasks"
DEBUG_TASKS = sorted(
    (t for t in load_tasks(TASKS_DIR) if "debug" in t.tags), key=lambda t: t.name
)


def test_debug_tasks_exist():
    assert [t.name for t in DEBUG_TASKS] == [
        "debug-hysteresis",
        "debug-pinlock",
        "debug-ringbuf",
    ]


@pytest.mark.parametrize("task", DEBUG_TASKS, ids=lambda t: t.name)
def test_buggy_listing_is_embedded_in_description(task):
    # Description integrity: the agent must see the deployed code it is asked
    # to fix, byte-for-byte inside the ```python fence.
    listing = (task.directory / "buggy.py").read_text(encoding="utf-8").rstrip("\n")
    assert "```python" in task.description
    assert listing in task.description, f"{task.name}: buggy.py drifted from the description"


@pytest.mark.parametrize("task", DEBUG_TASKS, ids=lambda t: t.name)
def test_golden_differs_from_buggy(task):
    solution = (task.directory / "solution.py").read_text(encoding="utf-8")
    buggy = (task.directory / "buggy.py").read_text(encoding="utf-8")
    assert solution != buggy


@pytest.mark.parametrize("task", DEBUG_TASKS, ids=lambda t: t.name)
def test_buggy_firmware_fails_its_own_task(task, tmp_path, wsl_unix_ready):
    # The point of a debug task: the shipped bug is detectable. The buggy
    # firmware is run as the entry - it must not pass (missing expects and/or
    # a fail pattern like 'alarm').
    if not wsl_unix_ready:
        pytest.skip("needs a WSL distro with micropython")
    buggy = dataclasses.replace(task, entry="buggy.py")
    res = run_task(buggy, out_dir=tmp_path / task.name)
    assert not res.passed, (
        f"{task.name}: the shipped buggy firmware passed its own task - "
        f"the task does not detect the bug (error={res.error!r})"
    )
