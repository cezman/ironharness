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
cannot garble the answer channel. Setpoint writes go through the shared
async stdin pump (IH-39): the backlog is capped at 1 MiB and overflowing it
is a run error - a controller that never reads stdin cannot hang the loop
past the wall deadline. Answers are read through a queue thread with the
wall deadline - silence/EOF/exit classify as run results, never as harness
crashes. Both pipes decode with errors="replace": hostile non-UTF8 output
degrades into the feedback log instead of killing the pump threads.
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


def _pump_lines(src, sink: queue.Queue, dropped: dict, *, max_lines: int = 8192) -> None:
    """Thread body: lines from src into sink (None on EOF). IH-45/IH-48: the
    sink is BOUNDED at max_lines - a controller flooding valid floats via
    sys.__stdout__/os.write (bypassing the stderr redirect) used to grow an
    unbounded queue for the whole loop. Past max_lines the OLDEST line is
    dropped and dropped['lines'] is counted: ipc_control aborts the run at
    the first step that sees the counter (a legit controller answers one
    line per step and never approaches the bound). EOF is never lost."""
    try:
        for line in src:
            if sink.qsize() >= max_lines:
                dropped["lines"] = dropped.get("lines", 0) + 1
                sink.get_nowait()  # drop the oldest, keep the bound
            sink.put_nowait(line)
        if sink.qsize() >= max_lines:
            # EOF into a full sink: the oldest line gives way, None must land
            dropped["lines"] = dropped.get("lines", 0) + 1
            sink.get_nowait()
        sink.put_nowait(None)
    finally:
        try:
            src.close()
        except OSError:
            pass


def _pump_stderr(src, sink: list, *, max_bytes: int = 262_144) -> None:
    """Thread body: accumulate stderr until the byte cap (controller
    prints + tracebacks; the thread is a daemon - it ends when the process
    dies). IH-25: the controller is untrusted - after the cap the pipe stops
    being drained, so a stderr flood BLOCKS the controller on write (killed
    by the wall deadline) instead of eating harness memory. Only the captured
    head lands in the feedback log anyway. IH-45: the cap is BYTES, not
    lines - a single giant line used to pass through whole; line boundaries
    are preserved for complete lines, the truncated tail is included as-is.
    """
    total = 0
    partial = ""
    try:
        while total < max_bytes:
            chunk = src.read(8192)
            if not chunk:
                if partial:
                    sink.append(partial)
                return
            total += len(chunk)
            buf = partial + chunk
            lines = buf.splitlines(keepends=True)
            partial = ""
            while lines and not lines[-1].endswith(("\n", "\r")):
                partial = lines.pop()
            sink.extend(lines)
        if partial:
            sink.append(partial)
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
    writer = None  # the async stdin pump; bound after the process spawns
    pump_thread = None  # the stdout pump thread; joined after the child dies
    dropped = None  # the pump's flood counter; bound after the process spawns
    stderr_lines: list[str] = []
    out_q: queue.Queue = queue.Queue(maxsize=8192)  # IH-48: the bound must live
    # HERE, at the production construction site - _pump_lines' drop-oldest
    # logic only fires on a bounded queue (the breaker pass found the bound
    # was dead code: the test built its own bounded queue)
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
        dropped: dict = {"lines": 0}  # stdout flood counter (IH-48), read per step
        pump_thread = threading.Thread(
            target=_pump_lines, args=(proc.stdout, out_q, dropped), daemon=True
        )
        pump_thread.start()
        err_thread = threading.Thread(
            target=_pump_stderr, args=(proc.stderr, stderr_lines), daemon=True
        )
        err_thread.start()
        # IH-39: the setpoint write used to be a synchronous stdin write - the
        # same hang class as the unix runner (a controller that never reads
        # stdin fills the pipe and blocks the loop past the wall deadline)
        writer = common._StdinWriter(proc.stdin)

        deadline = time.monotonic() + wall_timeout

        def ipc_control(t: float, y_meas: float, setpoint: float) -> float:
            """The closed-loop control adapter over the pipes; raises
            ControlAbort on silence, EOF, exit, or a non-numeric answer."""
            assert proc is not None and proc.stdin is not None
            writer.write(f"{t!r},{y_meas!r},{setpoint!r}\n")
            if writer.overflow():
                raise ControlAbort(
                    "controller does not read stdin: the setpoint write backlog "
                    f"exceeded the 1 MiB cap before step t={t:g}",
                    kind="run",
                )
            if writer.broken():
                raise ControlAbort(
                    _death_error(proc, stderr_lines, err_thread, f"before step t={t:g}"),
                    kind="run",
                )
            if dropped["lines"]:
                raise ControlAbort(
                    f"controller flooded the answer channel: {dropped['lines']} "
                    f"unread stdout line(s) dropped by step t={t:g} - control "
                    "must answer exactly one line per step",
                    kind="run",
                )
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
                except (ValueError, TypeError):
                    raise ControlAbort(
                        f"controller returned a non-number ({item.strip()[:60]!r}) "
                        f"at t={t:g} s - control must answer one float per step",
                        kind="run",
                    ) from None

        rows, error = run_closed_loop(ipc_control, spec)
        if error is None and dropped["lines"]:
            # a flood that ended before a per-step check saw it (a short loop)
            error = (
                f"controller flooded the answer channel: {dropped['lines']} "
                "unread stdout line(s) dropped - control must answer exactly "
                "one line per step"
            )
            error_kind = common.ERROR_RUN
        if error is None:
            # the controller survived the whole loop: stop the pump and close
            # stdin so it exits
            writer.close()
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
            if writer is not None:
                writer.close()
            if proc.poll() is None:
                # unblock a pump possibly stuck in a blocking write (IH-39)
                proc.kill()
            if pump_thread is not None:
                # let the stdout pump finish draining the pipe so the flood
                # counter is final before scoring reads it (IH-48)
                pump_thread.join(timeout=2)
            if proc.stdin is not None and (writer is None or writer.join(timeout=2)):
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            if exit_code is None:
                exit_code = proc.poll()

    metrics = compute_metrics(rows, spec)
    # IH-48: the deterministic flood verdict - after the finally, the child is
    # dead and the pump joined, so dropped["lines"] is final (the in-loop
    # per-step check above aborts promptly when it wins the race; this one
    # catches a flood that finished between steps)
    if error is None and dropped is not None and dropped["lines"]:
        error = (
            f"controller flooded the answer channel: {dropped['lines']} "
            "unread stdout line(s) dropped - control must answer exactly "
            "one line per step"
        )
        error_kind = common.ERROR_RUN
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
