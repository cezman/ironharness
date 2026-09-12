"""plant target runner (IH-25): the plant physics lives HERE, in the harness
process; the controller runs in its own process and talks to the plant over
pipes ((t, y_meas, setpoint) -> u).

Why two processes: the controller is untrusted agent code. In the old design
a single worker ran both the physics and the controller, so K/T travelled in
the worker's argv (spec file) - a spec-reading controller could cheat. Now
K/T never enter the controller process: no spec file on disk, no task-directory
paths in argv (the entry is copied into a throwaway cwd under a neutral name),
nothing in the environment. A controller that tries to read the parameters
finds nothing (pinned by a test). This is benchmark honesty, not a security
sandbox - the controller keeps the user's rights (an active filesystem search
outside its cwd is out of scope).

Protocol: the harness writes one step per line ("t,y_meas,setpoint"), the
controller answers one line (repr(u)) on stdout; controller print()s are
redirected to stderr by the bootstrap, so they land in the feedback log and
cannot garble the answer channel. The run loop writes stdin directly (one
~60-byte line in flight: a hung controller that never reads it cannot fill
the pipe) and reads answers through a queue thread with the wall deadline -
silence/EOF/exit classify as run results, never as harness crashes. Both
pipes decode with errors="replace": hostile non-UTF8 output degrades into
the feedback log instead of killing the pump threads.
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from ironbench import runner_common as common
from ironbench.plant import (
    ControlAbort,
    PlantSpec,
    check_requirements,
    compute_metrics,
    render_log,
    run_closed_loop,
)
from ironbench.tasks import Task

# The controller-process bootstrap: argv is [bootstrap, entry_basename] - a
# relative name resolved against the throwaway cwd, no task-directory paths.
# Controller print()s are redirected to stderr (the feedback log); the answer
# channel is the REAL stdout, which the controller cannot reach through
# sys.stdout anymore.
CONTROLLER_BOOTSTRAP = """\
import sys, runpy
sys.stdout = sys.stderr
ns = runpy.run_path(sys.argv[1], run_name="controller")
control = ns.get("control")
if not callable(control):
    print("BOOTSTRAP: entry file has no control(t, y, setpoint) function")
    sys.exit(2)
out = sys.__stdout__
for line in sys.stdin:
    t, y, sp = (float(v) for v in line.split(","))
    out.write(repr(control(t, y, sp)) + "\\n")
    out.flush()
"""


def _pump_lines(src, sink: queue.Queue) -> None:
    """Thread body: lines from src into sink (None on EOF)."""
    try:
        for line in src:
            sink.put(line)
    finally:
        try:
            src.close()
        except OSError:
            pass
        sink.put(None)


def _pump_stderr(src, sink: list) -> None:
    """Thread body: accumulate stderr until the 500-line cap (controller
    prints + tracebacks; the thread is a daemon - it ends when the process
    dies). IH-25: the controller is untrusted - after the cap the pipe stops
    being drained, so a stderr flood BLOCKS the controller on write (killed
    by the wall deadline) instead of eating harness memory. Only the captured
    head lands in the feedback log anyway."""
    try:
        for chunk in src:
            sink.extend(chunk.splitlines(keepends=True))
            if len(sink) >= 500:
                break
    finally:
        try:
            src.close()
        except OSError:
            pass


def _death_error(proc: subprocess.Popen, stderr_lines: list[str], err_thread, at: str) -> str:
    """Honest error for a controller process that died mid-run. The process
    status and stderr are settled first (at EOF the pumps may lag the death
    by a scheduling step): without the wait the exit code reads as None and
    the traceback is missing. The stderr first line is included only when it
    is not a traceback (the traceback's message lines - e.g. a SystemExit
    reason - stay in the feedback log, not in the error: the runner scans the
    error for infra phrases)."""
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    if err_thread is not None:
        err_thread.join(timeout=1.0)
    rc = proc.poll()
    all_stderr = "".join(stderr_lines)
    if rc != 0 and "Traceback" in all_stderr[:2000]:
        # a crash: the reason lives in the feedback log (the traceback), and
        # the error stays free of the agent's exception text (infra scan)
        return f"controller crashed (exit code {rc}) {at}"
    # the only stderr content allowed into the error is the bootstrap's own
    # marked diagnostic (a missing control function); everything else -
    # controller prints, SystemExit messages - stays in the feedback log
    for ln in (l.strip() for l in stderr_lines):
        if ln.startswith("BOOTSTRAP:"):
            return f"controller exited (code {rc}) {at}: {ln[:200]}"
    return f"controller exited (code {rc}) {at}"


def _run_plant(
    task: Task,
    *,
    out_dir: Path,
    journal=None,
) -> common.TaskResult:
    """Two-process closed loop: the harness simulates the plant, the
    controller answers (t, y, setpoint) -> u over pipes. The controller
    process is always the entry file under the neutral bootstrap (IH-25)."""
    run_dir, _ = common._new_run_dir(out_dir, task)
    serial_log = run_dir / f"{task.name}.serial.log"
    controller_cwd = run_dir / "controller-cwd"
    controller_cwd.mkdir(exist_ok=True)
    wall_timeout = task.timeout_sec * 2 + common.WALL_GRACE_SEC
    start = time.monotonic()
    exit_code: int | None = None
    error: str | None = None
    error_kind = common.ERROR_NONE
    rows: list = []
    spec = PlantSpec.from_section(task.plant)

    entry_src = task.directory / task.entry
    if not entry_src.is_file():
        result = common.TaskResult(
            task=task.name,
            passed=False,
            exit_code=None,
            duration_sec=0.0,
            serial_log=None,
            missed=tuple(task.expect),
            error=f"not found: entry file not found: {entry_src}",
            error_kind=common.ERROR_INFRA,
        )
        common._journal_result(journal, result)
        return result
    (controller_cwd / "controller_entry.py").write_bytes(entry_src.read_bytes())

    cmd = [sys.executable, "-c", CONTROLLER_BOOTSTRAP, "controller_entry.py"]
    if journal:
        journal("task_start", {"task": task.name})
    proc: subprocess.Popen | None = None
    stderr_lines: list[str] = []
    out_q: queue.Queue = queue.Queue()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(controller_cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        threading.Thread(target=_pump_lines, args=(proc.stdout, out_q), daemon=True).start()
        err_thread = threading.Thread(
            target=_pump_stderr, args=(proc.stderr, stderr_lines), daemon=True
        )
        err_thread.start()

        deadline = time.monotonic() + wall_timeout

        def ipc_control(t: float, y_meas: float, setpoint: float) -> float:
            """The closed-loop control adapter over the pipes; raises
            ControlAbort on silence, EOF, exit, or a non-numeric answer."""
            assert proc is not None and proc.stdin is not None
            try:
                proc.stdin.write(f"{t!r},{y_meas!r},{setpoint!r}\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                raise ControlAbort(
                    _death_error(proc, stderr_lines, err_thread, f"before step t={t:g}"),
                    kind="run",
                ) from None
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ControlAbort(
                        f"controller did not answer step t={t:g} (wall limit "
                        f"{wall_timeout:g} s exceeded)",
                        kind="timeout",
                    )
                try:
                    item = out_q.get(timeout=min(1.0, remaining))
                except queue.Empty:
                    continue
                if item is None:
                    raise ControlAbort(
                        _death_error(proc, stderr_lines, err_thread, f"at step t={t:g}"),
                        kind="run",
                    )
                try:
                    return float(item)
                except ValueError:
                    raise ControlAbort(
                        f"controller returned a non-number ({item.strip()[:60]!r}) "
                        f"at t={t:g} s - control must answer one float per step",
                        kind="run",
                    ) from None

        rows, error = run_closed_loop(ipc_control, spec)
        if error is None:
            # the controller survived the whole loop: close stdin so it exits
            try:
                proc.stdin.close()  # type: ignore[union-attr]
            except OSError:
                pass
            try:
                exit_code = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                exit_code = None
    except ControlAbort as e:
        error = str(e)
        error_kind = common.ERROR_TIMEOUT if e.kind == "timeout" else common.ERROR_RUN
    finally:
        if proc is not None:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            if exit_code is None:
                exit_code = proc.poll()

    metrics = compute_metrics(rows, spec)
    missed: list[str] = check_requirements(metrics, spec.requirements) if metrics else []
    if error is None and exit_code not in (0, None):
        error = f"controller process exited with code {exit_code}"
        error_kind = common.ERROR_RUN

    controller_output = "".join(stderr_lines)
    serial_log.write_text(
        render_log(spec, rows, metrics, missed, error, controller_output),
        encoding="utf-8",
    )
    log_text = serial_log.read_text(encoding="utf-8", errors="replace")
    missed_patterns, hit_fail = common._check_patterns(log_text, task.expect, task.fail)
    duration = round(time.monotonic() - start, 2)
    passed = not missed and not missed_patterns and not hit_fail and error is None
    result = common.TaskResult(
        task=task.name,
        passed=passed,
        exit_code=exit_code,
        duration_sec=duration,
        serial_log=serial_log,
        missed=tuple(missed) + missed_patterns,
        hit_fail=hit_fail,
        error=error,
        error_kind=error_kind,
    )
    common._journal_result(journal, result)
    return result
