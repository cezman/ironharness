"""real target: a live board, MicroPython REPL over SerialTransport (see
ironbench/realhw.py). Live hardware is opt-in per the sim-before-real
convention (IRONBENCH_REAL_PORT must be set).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from io_core.serial_transport import SerialTransport
from ironbench import runner_common as common
from ironbench.realhw import RealRepl
from ironbench.tasks import Task


def _run_real(
    task: Task,
    *,
    out_dir: Path,
    transport=None,
    port: str | None = None,
    journal=None,
) -> common.TaskResult:
    """Live board: MicroPython REPL over SerialTransport (see ironbench/realhw.py).

    On an ESP32 with a USB-UART bridge the REPL and the application UART share
    one line, so semantics follow the unix target: the entry is pasted into
    the REPL in raw-paste mode (Ctrl+E/Ctrl+D), the stimulus drives
    delay/write-serial/wait-serial, and expect/fail score the accumulated
    output. wait-serial answers are anchored to their stimulus write by the
    IH-14 chunk-stamp mechanism ported in IH-33 (see the canonical verdict
    below the stimulus loop). Port: real_transport (tests) -> real_port -> env
    IRONBENCH_REAL_PORT; without a port - an infra error (live hardware is
    opt-in per the sim-before-real convention). set-control/mqtt/noise are
    unsupported (honest error).
    """
    unsupported = None
    if "set-control" in {k for step in task.stimulus for k in step}:
        unsupported = "real target does not support set-control (Wokwi buttons/sensors)"
    elif task.mqtt:
        unsupported = "real target does not support the mqtt section yet"
    elif task.noise:
        unsupported = "real target does not support noise injection (the link is physical)"
    if unsupported:
        result = common.TaskResult(
            task=task.name,
            passed=False,
            exit_code=None,
            duration_sec=0.0,
            serial_log=None,
            missed=tuple(task.expect),
            error=unsupported,
            error_kind=common.ERROR_INFRA,
        )
        common._journal_result(journal, result)
        return result

    if transport is None:
        port = port or os.environ.get("IRONBENCH_REAL_PORT")
        if not port:
            result = common.TaskResult(
                task=task.name,
                passed=False,
                exit_code=None,
                duration_sec=0.0,
                serial_log=None,
                missed=tuple(task.expect),
                error=(
                    "IRONBENCH_REAL_PORT is not set — live-hardware runs are opt-in "
                    "(set the env var to the board's COM/tty port)"
                ),
                error_kind=common.ERROR_INFRA,
            )
            common._journal_result(journal, result)
            return result

        def transport_factory():
            t = SerialTransport(port, baudrate=115200, timeout=0.5)
            t.open()
            return t
    else:
        def transport_factory():
            return transport  # test injection of a fake

    run_dir, _ = common._new_run_dir(out_dir, task)
    serial_log = run_dir / f"{task.name}.serial.log"
    wall_timeout = task.timeout_sec * 2 + common.WALL_GRACE_SEC
    start = time.monotonic()
    error: str | None = None
    error_kind = common.ERROR_NONE
    serial_text = ""
    repl: RealRepl | None = None
    try:
        code = (task.directory / task.entry).read_text(encoding="utf-8")
        if journal:
            journal("task_start", {"task": task.name})
        repl = RealRepl(transport_factory())
        deadline = time.monotonic() + wall_timeout
        repl.boot(code)
        plain = tuple(p for p in task.expect if common._plain_text(p))
        # every wait-serial step as (needle, trigger stamp) - the canonical
        # verdict replays this sequence over the settled final log (IH-33,
        # the unix mechanism: no in-loop verdict, so no race with ingestion)
        waited: list[tuple[str, float | None]] = []
        last_trigger_stamp: float | None = None
        for step in task.stimulus:
            if time.monotonic() > deadline:
                break
            if "delay" in step:
                time.sleep(min(common._parse_delay(step["delay"]), max(0.0, deadline - time.monotonic())))
            elif "wait-serial" in step:
                needle = str(step["wait-serial"])
                waited.append((needle, last_trigger_stamp))
                while True:
                    stamp = common.first_occurrence_stamp(
                        repl.output(), repl.chunks(), needle, last_trigger_stamp
                    )
                    if stamp is not None or needle in repl.output():
                        break
                    if time.monotonic() >= deadline:
                        break  # no answer at all - an honest miss
                    time.sleep(0.05)
                if stamp is None:
                    break  # no genuine answer in this segment - the rest is pointless
            elif "write-serial" in step:
                # the REPL line editor ends input() on \r (\n is silent) -
                # normalize to \r, mirroring the unix target where \n is needed
                raw = str(step["write-serial"]).replace("\r\n", "\r").replace("\n", "\r")
                # drain the board buffer BEFORE recording the anchor point:
                # _pump is lazy (ingestion stamps are pump times), so anything
                # already sitting in the driver buffer must be ingested with a
                # pre-trigger stamp or it would masquerade as a fresh answer
                repl.output()
                last_trigger_stamp = time.monotonic()
                repl.write(raw.encode("utf-8"))
        # keep reading: until all literal expects are collected or the deadline
        # (an infinite firmware loop is normal)
        while time.monotonic() < deadline:
            if plain and all(p in repl.output() for p in plain):
                break
            time.sleep(0.05)
        serial_text = repl.output()
        chunks = repl.chunks()
        # Canonical anti-cheat verdict (IH-14 mechanism on real since IH-33):
        # a waited needle whose first occurrence was ingested before its
        # stimulus write is a dump. Honest-fast-answer races are impossible by
        # construction: the answer's chunk is stamped at ingestion, which
        # happens after the trigger stamp by the drain-before-anchor order.
        # Accepted residual (documented, like the IH-14 wokwi boundary): a
        # REACTIVE cheater - one that waits for the trigger line and then
        # prints plausible constants without ever touching the sensor - still
        # passes: the serial line alone cannot prove the sensor was read.
        # True verification needs out-of-band evidence (logic analyzer,
        # cross-sensor checks); the boot-dumper class is covered here.
        if error is None:
            for needle, since in waited:
                if needle in serial_text and common.first_occurrence_stamp(
                    serial_text, chunks, needle, since
                ) is None:
                    error = (
                        f"anti-cheat: {needle!r} was printed before the stimulus "
                        "asked for it (pre-printed output)"
                    )
                    error_kind = common.ERROR_RUN
                    break
    except (OSError, ConnectionError, ValueError, AssertionError) as e:
        error = f"failed to talk to the board: {e}"
        error_kind = common.ERROR_INFRA
    finally:
        if repl is not None:
            repl.close()

    serial_log.write_text(serial_text, encoding="utf-8")
    duration = round(time.monotonic() - start, 2)
    missed, hit_fail = common._check_patterns(serial_text, task.expect, task.fail)
    passed = not missed and not hit_fail and error is None
    result = common.TaskResult(
        task=task.name,
        passed=passed,
        exit_code=None,
        duration_sec=duration,
        serial_log=serial_log if serial_text else None,
        missed=missed,
        hit_fail=hit_fail,
        error=error,
        error_kind=error_kind,
    )
    common._journal_result(journal, result)
    return result
