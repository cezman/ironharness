"""IH-38: the CLI --target override (dataclasses.replace AFTER load_task) used
to bypass every per-target validation: `p-regulator --target unix` executed a
plant task on unix with zero scoring detectors (expect empty, no events, the
plant requirements not executed) and PASSED an arbitrary program. The swap
must be re-validated before the run - a task whose declared detectors the
target does not execute is refused, never silently run to a vacuous PASS.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

import ironbench.runner as runner_module
from ironbench.runner import run_task
from ironbench.tasks import load_task

TASKS_DIR = Path(__file__).resolve().parents[1] / "src" / "ironbench" / "tasks"


def _task(name: str):
    return load_task(TASKS_DIR / name)


def test_target_swap_without_detectors_cannot_pass(tmp_path):
    """The reviewer repro: a plant task on unix, the executed program is
    `pass` (a trivial local process standing in for micropython) - a PASS
    here means the run carried no working detector at all."""
    task = dataclasses.replace(_task("p-regulator"), target="unix")
    res = run_task(
        task,
        out_dir=tmp_path / "out",
        unix_cmd=[sys.executable, "-c", "pass"],
    )
    assert not res.passed, f"vacuous PASS on a detector-less target swap: {res}"
    assert res.error_kind == runner_module.ERROR_INFRA
    assert res.error and "not compatible" in res.error


def test_check_target_compat_refuses_swap_with_reason():
    from ironbench.tasks import check_target_compat

    with pytest.raises(ValueError, match="not compatible with target 'unix'"):
        check_target_compat(dataclasses.replace(_task("p-regulator"), target="unix"))


def test_check_target_compat_native_tasks_all_pass():
    from ironbench.tasks import check_target_compat, load_tasks

    for task in load_tasks(TASKS_DIR):
        check_target_compat(task)


def test_check_target_compat_meaningful_swap_passes_events_swap_refused():
    from ironbench.tasks import TASK_TARGETS, check_target_compat

    # uart-echo on unix: expect patterns + write-serial/delay steps - all
    # executed by the unix runner, the free local run stays possible.
    check_target_compat(dataclasses.replace(_task("uart-echo"), target="unix"))
    # blink-unix moved to wokwi: the events detector is unix-only - refused.
    with pytest.raises(ValueError):
        check_target_compat(dataclasses.replace(_task("blink-unix"), target="wokwi"))
    assert "real" in TASK_TARGETS  # the audit trail: real stays a legal target


def test_cli_refuses_incompatible_target(tmp_path, capsys):
    from ironbench.cli import main

    rc = main(["run", "--task", "p-regulator", "--target", "unix", "--out", str(tmp_path)])
    assert rc == 2
    assert "not compatible" in capsys.readouterr().out


def test_cli_solve_refuses_incompatible_target(tmp_path, capsys):
    # IH-38 review N3: the solve site refuses before any LLM config resolution,
    # so the negative path is testable with no LLM environment.
    from ironbench.cli import main

    rc = main(["solve", "--task", "p-regulator", "--target", "unix", "--out", str(tmp_path)])
    assert rc == 2
    assert "not compatible" in capsys.readouterr().out


def test_runtime_infra_refusal_survives_for_programmatic_mutation(tmp_path):
    """The set-control/unix authoring error moved to load_task (review N1), but
    a Task mutated programmatically AFTER load must still get the clean infra
    refusal from run_task - the solve loop depends on error_kind=infra."""
    from ironbench.tasks import check_target_compat

    task = dataclasses.replace(
        _task("uart-echo"),
        target="unix",
        stimulus=({"set-control": "button0: true"},),
    )
    with pytest.raises(ValueError, match="not compatible"):
        check_target_compat(task)
    res = run_task(task, out_dir=tmp_path / "out", unix_cmd=[sys.executable, "-c", "pass"])
    assert not res.passed
    assert res.error_kind == runner_module.ERROR_INFRA
    assert "set-control" in (res.error or "")
