"""plant target (see ironbench/plant.py): a closed "first-order plant +
controller" loop entirely in Python, no simulators or WSL. The worker runs the
controller (entry) in a separate process; scoring uses the step-response
metrics from task.yaml (missed = unmet requirements as human-readable strings).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from ironbench import runner_common as common
from ironbench.tasks import Task


def _run_plant(
    task: Task,
    *,
    out_dir: Path,
    plant_cmd: str | list | None = None,
    journal=None,
) -> common.TaskResult:
    """A run through the `python -m ironbench.plant` worker: it executes the
    controller (entry) in a separate process and writes the log + result.json.
    A hung/crashed controller is a run result (feedback to the agent), not a
    runner crash. plant_cmd is the injection point for tests. The target is
    local: no WSL, simulators, or their quotas needed. Artifacts land in a
    fresh per-run directory and result.json is stamped with run_id: a stale
    file from a previous run in the same out_dir is rejected, not scored."""
    run_dir, run_id = common._new_run_dir(out_dir, task)
    serial_log = run_dir / f"{task.name}.serial.log"
    result_file = run_dir / f"{task.name}.plant-result.json"
    controller_cwd = run_dir / "controller-cwd"
    controller_cwd.mkdir(exist_ok=True)  # tolerate a reused run dir (run_id check still guards scoring)
    wall_timeout = task.timeout_sec * 2 + common.WALL_GRACE_SEC
    start = time.monotonic()
    exit_code: int | None = None
    error: str | None = None
    error_kind = common.ERROR_NONE
    report: dict | None = None
    try:
        entry = task.directory / task.entry
        if not entry.is_file():
            raise FileNotFoundError(f"entry file not found: {entry}")
        spec_file = run_dir / f"{task.name}.plant.json"
        spec_file.write_text(json.dumps(task.plant, ensure_ascii=False), encoding="utf-8")
        if plant_cmd is None:
            cmd = [
                sys.executable,
                "-m",
                "ironbench.plant",
                "--entry",
                str(entry),
                "--spec",
                str(spec_file),
                "--log",
                str(serial_log),
                "--result",
                str(result_file),
                "--run-id",
                run_id,
            ]
        else:
            cmd = [plant_cmd] if isinstance(plant_cmd, str) else list(plant_cmd)
        if journal:
            journal("task_start", {"task": task.name})
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=wall_timeout,
            check=False,
            cwd=controller_cwd,
        )
        exit_code = proc.returncode
        if proc.returncode != 0:
            # the worker returns 0 even when the controller crashed - a non-zero
            # code means a problem in the worker/spec itself (not the agent's fault)
            error = (
                f"plant worker exited with code {proc.returncode}: "
                + (proc.stderr or proc.stdout or "").strip()[-300:]
            )
            error_kind = common.ERROR_INFRA
    except subprocess.TimeoutExpired:
        error = f"the plant run exceeded the wall limit ({wall_timeout} s) - controller stuck in a loop?"
        error_kind = common.ERROR_TIMEOUT
    except FileNotFoundError as e:
        error = f"not found: {e.filename or e}"
        error_kind = common.ERROR_INFRA

    if error is None:
        if result_file.is_file():
            try:
                report = json.loads(result_file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                error = f"failed to parse the plant worker result.json: {e}"
                error_kind = common.ERROR_INFRA
        else:
            error = "the plant worker left no result.json"
            error_kind = common.ERROR_INFRA
    if report is not None and not isinstance(report, dict):
        error = "plant worker result.json is not a JSON object"
        error_kind = common.ERROR_INFRA
        report = None
    if report is not None and report.get("run_id") != run_id:
        # A file at the result path that this run did not request is hostile
        # input, not data: its metrics must never be scored.
        error = (
            f"plant result.json is not from this run "
            f"(run_id {report.get('run_id')!r} != {run_id!r})"
        )
        error_kind = common.ERROR_INFRA
        report = None
    if report and report.get("error"):
        # error carries only the headline: the full traceback stays in the log
        # (feedback to the agent), and the agent's exception text must not leak
        # into is_infra_error
        error = report["error"].splitlines()[0]
        error_kind = common.ERROR_RUN

    serial_text = (
        serial_log.read_text(encoding="utf-8", errors="replace") if serial_log.is_file() else ""
    )
    missed_metrics = tuple(report.get("missed", ())) if report else ()
    missed_patterns, hit_fail = common._check_patterns(serial_text, task.expect, task.fail)
    missed = missed_metrics + missed_patterns
    passed = not missed and not hit_fail and error is None
    result = common.TaskResult(
        task=task.name,
        passed=passed,
        exit_code=exit_code,
        duration_sec=round(time.monotonic() - start, 2),
        serial_log=serial_log if serial_text else None,
        missed=missed,
        hit_fail=hit_fail,
        error=error,
        error_kind=error_kind,
    )
    common._journal_result(journal, result)
    return result
