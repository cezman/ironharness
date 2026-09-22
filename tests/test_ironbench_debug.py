"""Golden debug-class tasks (IH-7): the shipped buggy firmware must FAIL its
own task (the planted bug is detectable), and the buggy listing must reach the
agent - the solve loop never reads files, so the description's embedded code
block is the agent's only input and must carry buggy.py verbatim. The fixed
goldens are pinned live (WSL micropython) by the anti-cheat suite's golden
regression (GOLDEN_UNIX); the wokwi debug twin (IH-84) is pinned offline on a
fake wokwi-cli and live only inside the quota-gated integration suite."""

from __future__ import annotations

import dataclasses
import os
import re
import shutil
import sys
import textwrap
from pathlib import Path

import pytest

from ironbench.runner import run_task
from ironbench.tasks import load_task, load_tasks

TASKS_DIR = Path(__file__).parents[1] / "src" / "ironbench" / "tasks"
DEBUG_TASKS = sorted(
    (t for t in load_tasks(TASKS_DIR) if "debug" in t.tags), key=lambda t: t.name
)


def test_debug_tasks_exist():
    assert [t.name for t in DEBUG_TASKS] == [
        "debug-heartbeat",
        "debug-hysteresis",
        "debug-pinlock",
        "debug-ringbuf",
        "debug-station",
    ]


@pytest.mark.parametrize("task", DEBUG_TASKS, ids=lambda t: t.name)
def test_buggy_listing_is_embedded_in_description(task):
    # Description integrity: the agent must see the deployed code it is asked
    # to fix, byte-for-byte as the single ```python fence of the description.
    listing = (task.directory / "buggy.py").read_text(encoding="utf-8").rstrip("\n")
    fences = re.findall(r"```python\n(.*?)\n```", task.description, re.DOTALL)
    assert fences == [listing], f"{task.name}: buggy.py drifted from the description"


@pytest.mark.parametrize("task", DEBUG_TASKS, ids=lambda t: t.name)
def test_golden_differs_from_buggy(task):
    solution = (task.directory / "solution.py").read_text(encoding="utf-8")
    buggy = (task.directory / "buggy.py").read_text(encoding="utf-8")
    assert solution != buggy


def test_debug_station_fix_is_not_leaked_by_the_description():
    # IH-97: the datasheet register map ships in the notes (reference data,
    # IH-65 knowledge-before-the-error), but the description itself - the
    # runner-visible prompt - must not hand over the corrected read
    task = next(t for t in DEBUG_TASKS if t.name == "debug-station")
    assert "0x88" not in task.description
    assert task.target == "real"
    assert task.tags == ("debug",)


def test_wokwi_debug_fix_is_not_leaked_by_the_description():
    # IH-84: the description carries the symptom (the NameError text), not
    # the fix - the corrected line "count = 0" stays out of the prompt
    task = next(t for t in DEBUG_TASKS if t.name == "debug-heartbeat")
    assert "count = 0" not in task.description
    assert task.target == "wokwi"
    assert task.tags == ("debug",)


@pytest.mark.parametrize("task", DEBUG_TASKS, ids=lambda t: t.name)
def test_buggy_firmware_fails_its_own_task(task, tmp_path, wsl_unix_ready):
    # The point of a debug task: the shipped bug is detectable. The buggy
    # firmware is run as the entry - it must not pass (missing expects and/or
    # a fail pattern like 'alarm').
    if task.target != "unix":
        pytest.skip(
            "unix-runner detectability; non-unix debug goldens pin it against "
            "their own fakes (test_ironbench_real.py) and live validation"
        )
    if not wsl_unix_ready:
        pytest.skip("needs a WSL distro with micropython")
    buggy = dataclasses.replace(task, entry="buggy.py")
    res = run_task(buggy, out_dir=tmp_path / task.name)
    assert not res.passed, (
        f"{task.name}: the shipped buggy firmware passed its own task - "
        f"the task does not detect the bug (error={res.error!r})"
    )


# --- IH-84: the wokwi debug twin (debug-heartbeat), pinned offline on a fake
# wokwi-cli. The fake emits the honest wokwi log shape: the raw-paste echo of
# the entry source (cut by the IH-74 trim), then runtime output. The live
# quota run picks the task up through the gated integration suite. ---

FAKE_WOKWI_CLI = textwrap.dedent(
    """
    import os, sys
    args = sys.argv[1:]
    log = args[args.index("--serial-log-file") + 1]
    Path = __import__("pathlib").Path
    Path(log).write_text(os.environ["FAKE_SERIAL"], encoding="utf-8")
    sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
    """
)


def _run_wokwi_fake(tmp_path, task, serial, exit_code=0):
    cli = [sys.executable, "-c", FAKE_WOKWI_CLI]
    saved = {k: os.environ.pop(k, None) for k in ("FAKE_SERIAL", "FAKE_EXIT", "WOKWI_CLI_TOKEN")}
    os.environ.update({"FAKE_SERIAL": serial, "FAKE_EXIT": str(exit_code)})
    try:
        return run_task(task, out_dir=tmp_path / "out", cli_path=cli)
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def _wokwi_log(entry_source: str, runtime: str) -> str:
    """The honest wokwi paste log: the raw-paste mode marker, the echoed
    source (what the IH-74 trim cuts), then runtime output."""
    return "paste mode; press Ctrl+E to paste\n" + entry_source + runtime


def _task(name: str):
    return next(t for t in DEBUG_TASKS if t.name == name)


def test_wokwi_debug_golden_passes_on_honest_fake_runtime(tmp_path):
    # The reference run: echo trim engages, the anchors (input echo lines)
    # precede the answers, boot output is declared - the task config must
    # credit the honest runtime end-to-end.
    task = _task("debug-heartbeat")
    source = (task.directory / task.entry).read_text(encoding="utf-8")
    runtime = "hb ready\ngo\nbeat 1\ngo\nbeat 2\ngo\nbeat 3\n"
    res = _run_wokwi_fake(tmp_path, task, _wokwi_log(source, runtime), exit_code=0)
    assert res.passed, f"missed={res.missed} hit_fail={res.hit_fail} error={res.error!r}"


def test_wokwi_debug_buggy_firmware_fails_its_own_task(tmp_path):
    # The point of a debug task, wokwi edition: the buggy firmware as the
    # entry dies with the genuine MicroPython traceback in the runtime log -
    # the fail patterns must condemn it (and the DIAGNOSIS parser gets the
    # text for the repair loop).
    task = _task("debug-heartbeat")
    buggy = dataclasses.replace(task, entry="buggy.py")
    source = (task.directory / "buggy.py").read_text(encoding="utf-8")
    runtime = (
        "hb ready\n"
        "go\n"
        "Traceback (most recent call last):\n"
        '  File "<stdin>", line 6, in <module>\n'
        "NameError: name 'count' isn't defined\n"
    )
    res = _run_wokwi_fake(tmp_path, buggy, _wokwi_log(source, runtime), exit_code=42)
    assert not res.passed, "the shipped buggy firmware passed its own task"
    assert "Traceback" in res.hit_fail
    assert "name 'count' isn't defined" in res.hit_fail
    assert "beat 1" in res.missed


def test_wokwi_debug_dump_cheater_fails_with_preprinted(tmp_path):
    # A submission that prints every expected line at boot (no protocol) must
    # fail: the first input echo anchors the check, and the answers printed
    # before it are condemned (fail-closed, audit A semantics).
    src = next(t for t in DEBUG_TASKS if t.name == "debug-heartbeat")
    stage = tmp_path / "t"
    shutil.copytree(src.directory, stage)
    (stage / "cheat.py").write_text(
        'print("hb ready")\nprint("beat 1")\nprint("beat 2")\nprint("beat 3")\n'
        "while True:\n    input()\n",
        encoding="utf-8",
    )
    cheat = dataclasses.replace(load_task(stage), entry="cheat.py")
    source = (stage / "cheat.py").read_text(encoding="utf-8")
    runtime = "hb ready\nbeat 1\nbeat 2\nbeat 3\ngo\ngo\ngo\n"
    res = _run_wokwi_fake(tmp_path, cheat, _wokwi_log(source, runtime), exit_code=0)
    assert not res.passed, "a print-everything cheater passed the debug task"
    assert res.error is not None and "pre-printed" in res.error


def test_wokwi_debug_notes_carry_the_repl_knowledge():
    # IH-65 knowledge-before-the-error: the notes must teach the wokwi REPL
    # facts the repair loop relies on (paste mode, \r-only enter, NameError).
    task = _task("debug-heartbeat")
    joined = " ".join(task.notes)
    assert "raw-paste" in joined
    assert "\\r" in joined or "Ctrl+E" in joined
    assert "NameError" in joined
