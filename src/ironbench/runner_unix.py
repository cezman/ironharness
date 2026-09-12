"""unix target: the MicroPython unix port in WSL2 (free local runs).

No REPL, no paste: the script executes as a file and input() reads our stdin -
so only pure-serial tasks qualify (machine/dht unavailable). The mqtt section
starts an mqtt_sim broker next to the firmware (see io_core/mqtt_sim.py), the
harness talks to it as a client (mqtt-publish/mqtt-collect steps), and every
received publish is appended to the serial log as "mqtt: <topic> <payload>".
"""

from __future__ import annotations

import itertools
import os
import random
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

from io_core.faults import FaultyTransport
from io_core.mqtt_transport import MqttTransport
from ironbench import runner_common as common
from ironbench import runner_renode
from ironbench.tasks import Task


def _check_events(serial_text: str, events: tuple[dict, ...]) -> tuple[str, ...]:
    """Timing-aware scoring over shim event lines (IH-16): for every event
    spec, the count of matched lines (whose trailing token is a millisecond
    timestamp) and every consecutive interval between them must satisfy the
    declared bounds. A cheater printing bare expected strings produces no
    parseable events and fails the count.

    Accepted residual (narrowed by IH-24): a cheater that SIMULATES the shim
    line format with well-timed fake timestamps now ALSO fails when the run
    carries chunk-stamp anchors (see _check_events_realtime) - fake lines
    printed in one dump are ingested within milliseconds, which violates the
    declared period. Only output paced like real hardware passes; the
    residual is a simulated device paced by real sleeps. Passive tasks (no
    stimulus triggers) keep the old text-only scoring."""
    missed: list[str] = []
    for ev in events:
        stamps: list[float] = []
        for line in serial_text.splitlines():
            m = re.search(ev["pattern"], line)
            if not m:
                continue
            tail = line[m.end():].split()
            if not tail:
                continue
            try:
                stamps.append(float(tail[-1]))
            except ValueError:
                continue
        name = f"events[{ev['pattern']}]"
        if len(stamps) < ev["count_min"]:
            missed.append(f"{name}: {len(stamps)} events < required {ev['count_min']}")
            continue
        period = ev.get("period_ms")
        if period:
            for a, b in itertools.pairwise(stamps):
                delta = b - a
                if not period[0] <= delta <= period[1]:
                    missed.append(
                        f"{name}: period {delta:.0f}ms outside "
                        f"[{period[0]:g}, {period[1]:g}]"
                    )
                    break
    return tuple(missed)


# Ingestion lag slack for the real-time event anchor (IH-24): the wall-clock
# gap between two ingested chunks may exceed the declared period by this much
# (reader scheduling, pipe buffering) and fall short of it by no less.
EVENT_STAMP_TOLERANCE_SEC = 0.15


def _check_events_realtime(
    box, events: tuple[dict, ...]
) -> tuple[str, ...]:
    """Chunk-stamp anchor for event scoring (IH-24): re-checks the period
    bounds against the REAL ingestion stamps of the chunks carrying event
    lines, not only the firmware-embedded timestamps. A fake-timestamp dump
    printed in one burst is ingested within milliseconds, so its real
    intervals violate any plausible period - the declared bounds are
    physically unverifiable for a dump. Honest firmware paced by sleeps
    ingests line-by-line with real gaps and passes with the tolerance.
    Returns the same missed-strings shape as _check_events."""
    missed: list[str] = []
    for ev in events:
        period = ev.get("period_ms")
        if not period:
            continue
        stamps: list[float] = []
        with box["lock"]:
            for chunk_stamp, chunk in box["chunks"]:
                for line in chunk.splitlines():
                    if re.search(ev["pattern"], line):
                        stamps.append(chunk_stamp)
        name = f"events[{ev['pattern']}] (real-time)"
        if len(stamps) < ev["count_min"]:
            continue  # the count/anchor mismatch is scored by _check_events
        lo = period[0] / 1000 - EVENT_STAMP_TOLERANCE_SEC
        hi = period[1] / 1000 + EVENT_STAMP_TOLERANCE_SEC
        for a, b in itertools.pairwise(stamps):
            delta = b - a
            if not lo <= delta <= hi:
                missed.append(
                    f"{name}: period {delta:.2f}s outside "
                    f"[{period[0]:g}, {period[1]:g}]ms (+/-{EVENT_STAMP_TOLERANCE_SEC:g}s) - "
                    "output was not paced like the declared hardware"
                )
                break
    return tuple(missed)


def _unix_cmd(remote_entry: str, env_prefix: str = "") -> list[str]:
    """micropython executes the entry directly: stdin = stimulus, stdout = serial log."""
    upy_bin = os.environ.get("IRONBENCH_UNIX_BIN", "~/bin/micropython")
    return [
        "wsl",
        "-d",
        common._wsl_distro(),
        "--",
        "bash",
        "-c",
        f"{env_prefix}exec {upy_bin} {remote_entry}",
    ]


class _StdinWriter:
    """Adapts the process stdin to the Transport.write protocol - the
    FaultyTransport target."""

    def __init__(self, stdin) -> None:
        self._stdin = stdin

    def write(self, data: bytes) -> None:
        assert self._stdin is not None
        self._stdin.write(data)
        self._stdin.flush()


# mqtt_sim lives next to the io_core sources (staged into WSL for the firmware)
MQTT_SIM_PATH = Path(__file__).resolve().parents[1] / "io_core" / "mqtt_sim.py"


def _free_tcp_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _start_wsl_mqtt_broker(task: Task, port: int) -> tuple[subprocess.Popen, int | None]:
    """Starts mqtt_sim in WSL2 (same localhost as the firmware); the Windows
    side reaches it via localhost-forwarding, hence the bind on 0.0.0.0.

    terminate() kills only wsl.exe - the linux process survives it (a zombie
    holding the port, see the zombie listener note in wsl-run.sh), so the
    broker prints its PID and cleanup finishes it with a kill by PID inside
    the distro."""
    remote_dir = f"{common.RENODE_REMOTE_ROOT}/{task.name}-mqtt"
    common._push_to_wsl(common._tar_of(MQTT_SIM_PATH, "mqtt_sim.py"), remote_dir, "BROKER-PUSHED")
    proc = subprocess.Popen(
        [
            "wsl",
            "-d",
            common._wsl_distro(),
            "--",
            "bash",
            "-c",
            (
                f"python3 {remote_dir}/mqtt_sim.py --host 0.0.0.0 --port {port} & "
                'echo "BROKER_PID=$!"; wait $!'
            ),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    # the same WSL-startup deadline as the renode target; read through the
    # renode module namespace so its patch point stays live (one namespace
    # per constant, like everything shared in the runner split)
    deadline = time.monotonic() + runner_renode.RENODE_CONNECT_SEC
    while True:
        try:
            probe = socket.create_connection(("localhost", port), timeout=1.0)
            probe.close()
            break
        except OSError:
            if proc.poll() is not None or time.monotonic() > deadline:
                _stop_broker_proc(proc, None)
                raise ConnectionError(f"mqtt_sim broker on :{port} did not come up") from None
            time.sleep(0.2)
    try:
        wsl_pid = int(common._read_marker_line(proc.stdout, "BROKER_PID", 5))
    except (ConnectionError, ValueError):
        wsl_pid = None  # without a PID, cleanup degrades to terminating wsl.exe
    return proc, wsl_pid


def _stop_broker_proc(proc: subprocess.Popen | None, wsl_pid: int | None) -> None:
    if proc is None:
        return
    if wsl_pid is not None:
        try:
            subprocess.run(
                ["wsl", "-d", common._wsl_distro(), "--", "kill", str(wsl_pid)],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    common._reap(proc)
    if proc.stdout is not None:
        proc.stdout.close()


def _first_answer_stamp(box, needle: str, since: float | None) -> float | None:
    """Ingestion stamp of the chunk holding the first occurrence of `needle`,
    or None when it is not printed yet / was printed unprompted (stamped
    before `since`; since=None = no stimulus write happened yet)."""
    with box["lock"]:
        pos = box["text"].find(needle)
        if pos < 0:
            return None
        seen = 0
        stamp_hit: float | None = None
        for stamp, chunk in box["chunks"]:
            seen += len(chunk)
            if pos < seen:
                stamp_hit = stamp
                break
    if stamp_hit is None or since is None:
        return None
    # Only a strictly-earlier stamp condemns: an ingestion stamp EQUAL to the
    # trigger stamp means write -> firmware read -> echo -> ingest completed
    # within one monotonic clock tick (field-proven on coarse-clock Windows
    # CI, where a legal fast answer was condemned as pre-printed). Same-tick
    # order is unknowable, so the benefit of the doubt goes to the agent; the
    # dump-and-exit cheater stays covered by the zero-waits rule.
    return stamp_hit if stamp_hit >= since else None


def _run_unix(
    task: Task,
    *,
    out_dir: Path,
    unix_cmd: str | list | None = None,
    mqtt_broker=None,
    journal=None,
) -> common.TaskResult:
    """Runs the entry with the unix-port micropython and scores the output.

    No REPL, no paste: the script executes as a file and input() reads our
    stdin - so only pure-serial tasks qualify (machine/dht unavailable).
    Enter on unix is \\n, so \\r from the wokwi-style stimulus is translated
    to \\n. The mqtt section: an mqtt_sim broker starts next to the firmware,
    the harness talks to it as a client (mqtt-publish/mqtt-collect steps), and
    every received publish is appended to the serial log as
    "mqtt: <topic> <payload>". unix_cmd=None -> the WSL path (the entry
    travels as a tar blob); command injection is a local fake for tests.
    mqtt_broker - an already-running broker (tests).
    """
    if "set-control" in {k for step in task.stimulus for k in step}:
        result = common.TaskResult(
            task=task.name,
            passed=False,
            exit_code=None,
            duration_sec=0.0,
            serial_log=None,
            missed=tuple(task.expect),
            error="set-control is not supported by the unix target (Wokwi buttons/sensors)",
            error_kind=common.ERROR_INFRA,
        )
        common._journal_result(journal, result)
        return result

    run_dir, _ = common._new_run_dir(out_dir, task)
    serial_log = run_dir / f"{task.name}.serial.log"
    wall_timeout = task.timeout_sec * 2 + common.WALL_GRACE_SEC
    start = time.monotonic()
    exit_code: int | None = None
    error: str | None = None
    error_kind = common.ERROR_NONE
    serial_text = ""
    proc = None
    mqtt_client: MqttTransport | None = None
    broker_proc: subprocess.Popen | None = None
    broker_pid: int | None = None
    mqtt_subscribed: set[str] = set()
    try:
        mqtt_port = 0
        if task.mqtt:
            mqtt_port = _free_tcp_port()
            if mqtt_broker is not None:
                mqtt_port = mqtt_broker.port  # test broker is already running
            else:
                broker_proc, broker_pid = _start_wsl_mqtt_broker(task, mqtt_port)
            mqtt_client = MqttTransport(
                "127.0.0.1",
                port=mqtt_port,
                client_id=str(task.mqtt.get("client_id") or f"ironbench-{task.name}"),
            )
            mqtt_client.open()
            if journal:
                journal("mqtt_broker_ready", {"task": task.name, "port": mqtt_port})
            # subscribe up front, before the firmware spawns: the first publish
            # (QoS0, not retained) goes out right after startup - subscribing at
            # the collect step could miss it
            for step in task.stimulus:
                if "mqtt-collect" in step:
                    topic = str(step["mqtt-collect"]["topic"])
                    if topic not in mqtt_subscribed:
                        mqtt_client.subscribe(topic)
                        mqtt_subscribed.add(topic)
        if unix_cmd is None:
            remote_dir = f"{common.RENODE_REMOTE_ROOT}/{task.name}-unix"
            if task.shim:
                # entry + shim travel together: sys.path[0] is the script dir,
                # so machine.py next to the entry resolves `import machine`;
                # the remote dir is cleaned first - a stale machine.py from a
                # run with a shim must not leak into a shim-less re-run
                common._push_to_wsl(
                    common._tar_pairs(
                        [
                            (task.directory / task.entry, task.entry),
                            (common.SHIMS_DIR / f"{task.shim}.py", f"{task.shim}.py"),
                        ]
                    ),
                    remote_dir,
                    "STAGE-PUSHED",
                    clean=True,
                )
            else:
                common._push_to_wsl(
                    common._tar_of(task.directory / task.entry, task.entry),
                    remote_dir,
                    "STAGE-PUSHED",
                    clean=True,
                )
            env_prefix = (
                f"IRONBENCH_MQTT_HOST=127.0.0.1 IRONBENCH_MQTT_PORT={mqtt_port} "
                if task.mqtt
                else ""
            )
            cmd = _unix_cmd(f"{remote_dir}/{task.entry}", env_prefix)
        else:
            if task.shim:
                # injected commands run the entry in place: the shim goes next
                # to it and overwrites any same-named file - the harness shim
                # is authoritative for a shim-declaring task (task dirs here
                # are per-run temporary directories in tests)
                shutil.copy2(common.SHIMS_DIR / f"{task.shim}.py", task.directory / f"{task.shim}.py")
            cmd = [unix_cmd] if isinstance(unix_cmd, str) else list(unix_cmd)
        if journal:
            journal("task_start", {"task": task.name})
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # the unix port writes tracebacks to stderr
        )
        box = {"text": "", "chunks": [], "eof": False, "lock": threading.Lock()}

        def _box_append(chunk: str) -> None:
            # text and chunks must stay position-consistent (the anti-cheat
            # maps a needle position back to its chunk); collect-appends and
            # the reader thread both land here under the same lock
            with box["lock"]:
                box["chunks"].append((time.monotonic(), chunk))
                box["text"] += chunk

        def reader():
            try:
                for line in iter(proc.stdout.readline, b""):
                    # the ingestion stamp powers the anti-cheat: a chunk cannot
                    # be ingested before it was printed, so "first occurrence
                    # of the needle stamped strictly before the stimulus write"
                    # proves the string was printed unprompted (a same-tick
                    # stamp is unknowable order - see _first_answer_stamp)
                    _box_append(line.decode("utf-8", "replace"))
            except OSError:
                pass
            finally:
                box["eof"] = True

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()
        deadline = time.monotonic() + wall_timeout
        plain = tuple(p for p in task.expect if common._plain_text(p))
        # noisy line: stimulus writes go through FaultyTransport (drop/corrupt
        # per the scenario from task.yaml); the seed pins the scenario - run
        # reproducibility is the point, this is not cryptography (see
        # io_core/faults.py). One op of the counter = one write-serial stimulus
        # step. Firmware output (stdout) is not noised: pattern scoring stays fair.
        writer = _StdinWriter(proc.stdin)
        if task.noise:
            writer = FaultyTransport(
                writer, task.noise.get("faults", []), rng=random.Random(task.noise.get("seed", 0))
            )
        waits_done = 0
        trigger_stamps: list[float] = []  # every delivered trigger (write/publish)
        # every wait-serial step as (needle, trigger stamp at the moment the
        # step was reached) - the canonical post-run verdict replays exactly
        # this sequence, so the verdict is a pure function of the settled
        # final state (log text, chunk stamps, recorded trigger stamps)
        waited: list[tuple[str, float | None]] = []
        last_trigger_stamp: float | None = None  # set by write-serial / mqtt-publish
        try:
            for step in task.stimulus:
                if time.monotonic() > deadline:
                    break
                if "wait-serial" in step:
                    needle = str(step["wait-serial"])
                    # anti-cheat (IH-14): the answer must be emitted after the
                    # stimulus write asked for it (see _first_answer_stamp).
                    # No verdict is made HERE: the in-loop classification used
                    # to race the reader thread and flip between the
                    # "pre-printed" and "zero-waits" wordings (IH-28 follow-up,
                    # closed in IH-23) - the verdict is decided once, over the
                    # final log, after the reader joins.
                    waited.append((needle, last_trigger_stamp))
                    # check-at-least-once: the stamp is evaluated BEFORE the
                    # eof/deadline exits, so an honest fast answer ingested
                    # before the firmware's EOF is still credited (an
                    # eof-first condition would convict it as a zero-waits dump)
                    stamp = None
                    while True:
                        stamp = _first_answer_stamp(box, needle, last_trigger_stamp)
                        if stamp is not None or needle in box["text"]:
                            break
                        if time.monotonic() >= deadline or box["eof"]:
                            break  # no answer at all - an honest miss
                        time.sleep(0.05)
                    if stamp is None:
                        break  # no genuine answer in this segment - the rest is pointless
                    waits_done += 1
                    continue
                if box["eof"]:
                    break  # the firmware is gone - no point driving further stimulus
                if "delay" in step:
                    time.sleep(
                        min(common._parse_delay(step["delay"]), max(0.0, deadline - time.monotonic()))
                    )
                elif "write-serial" in step:
                    last_trigger_stamp = time.monotonic()  # the anti-cheat anchor point
                    trigger_stamps.append(last_trigger_stamp)
                    raw = str(step["write-serial"]).replace("\r\n", "\n").replace("\r", "\n")
                    raw = raw.encode("utf-8")
                    if task.noise and raw.endswith(b"\n"):
                        # the terminator is not noised: corrupting \n would glue
                        # frames into a stream input() can never recover from, retry or not
                        writer.write(raw[:-1])
                        proc.stdin.write(b"\n")
                        proc.stdin.flush()
                    else:
                        writer.write(raw)
                elif "mqtt-publish" in step and mqtt_client is not None:
                    pub = step["mqtt-publish"]
                    # a retained/published command is what the firmware reacts to:
                    # it anchors the wait-serial anti-cheat just like write-serial
                    last_trigger_stamp = time.monotonic()
                    trigger_stamps.append(last_trigger_stamp)
                    mqtt_client.publish(
                        str(pub["topic"]),
                        str(pub["payload"]),
                        qos=int(pub.get("qos", 0)),
                        retain=bool(pub.get("retain", False)),
                    )
                elif "mqtt-collect" in step and mqtt_client is not None:
                    col = step["mqtt-collect"]
                    topic = str(col["topic"])
                    if topic not in mqtt_subscribed:
                        # the broker duplicates delivery on re-subscribe - subscribe once
                        mqtt_client.subscribe(topic)
                        mqtt_subscribed.add(topic)
                    need = int(col["count"])
                    got = 0
                    collect_deadline = time.monotonic() + float(col.get("timeout_sec", 10))
                    while (
                        got < need
                        and time.monotonic() < collect_deadline
                        and not box["eof"]
                    ):
                        msg = mqtt_client.read_message(timeout=0.2)
                        if msg is not None:
                            got += 1
                            _box_append(f"mqtt: {msg['topic']} {msg['payload']}\n")
                    if got < need:
                        # registered as a chunk too: the anti-cheat maps needle
                        # positions to chunks, an unregistered append would
                        # shift them and misattribute later answers
                        _box_append(
                            f"mqtt: collect {col['topic']}: received {got} of {need}\n"
                        )
            # keep reading: until all literal expects are collected, EOF, or the
            # deadline (an infinite firmware loop is normal, like --timeout in wokwi)
            while time.monotonic() < deadline and not box["eof"]:
                if plain and all(p in box["text"] for p in plain):
                    break
                time.sleep(0.05)
            matched = bool(plain) and all(p in box["text"] for p in plain)
            if box["eof"]:
                exit_code = proc.wait(timeout=5)
            elif matched or time.monotonic() >= deadline:
                try:
                    # a finite program may have exited on its own - reap the code
                    exit_code = proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    # infinite: kill it WITHOUT closing stdin - EOF at input()
                    # would print a Traceback into the serial log (a fail pattern)
                    proc.kill()
                    proc.wait(timeout=5)
                    exit_code = None
            else:
                try:
                    exit_code = proc.wait(timeout=3)  # a finite program exits on its own
                except subprocess.TimeoutExpired:
                    exit_code = None
            reader_thread.join(timeout=2)  # snapshot the log after the reader finishes
            serial_text = box["text"]
            # Canonical anti-cheat verdicts (IH-14, made deterministic in
            # IH-23): evaluated once, after the reader joined, over the final
            # log, in a fixed order - so the wording no longer depends on
            # reader-thread timing (the in-loop classification used to race
            # the reader and flip between the two wordings, IH-28).
            # (1) A waited needle whose FIRST occurrence was ingested strictly
            # before its stimulus write is a dump: the same first-occurrence
            # rule the in-loop wait applied, replayed on the settled chunk
            # list. (2) The firmware exited without crediting a single
            # wait-serial step. The reader ingests all output before flagging
            # EOF, so at EOF a wait-serial needle still sitting in the log was
            # never credited as a stimulus answer (a genuine answer is credited
            # at its wait step before EOF can be observed) - it was printed
            # unprompted, whether or not a trigger write got delivered later:
            # "pre-printed". A log without any waited needle is the plain
            # zero-waits dump (output without a stimulus answer is not
            # earned). A needle matching pre-answer output cannot happen for
            # a well-formed task: wait-serial literals are unique in the
            # log per the task-authoring convention.
            if error is None:
                for needle, since in waited:
                    if needle in serial_text and _first_answer_stamp(box, needle, since) is None:
                        error = (
                            f"anti-cheat: {needle!r} was printed before the "
                            "stimulus asked for it (pre-printed output)"
                        )
                        error_kind = common.ERROR_RUN
                        break
            if error is None and box["eof"] and waits_done == 0 and any(
                "wait-serial" in s for s in task.stimulus
            ):
                dumped = next(
                    (
                        needle
                        for needle in (
                            str(s["wait-serial"]) for s in task.stimulus if "wait-serial" in s
                        )
                        if needle in serial_text
                    ),
                    None,
                )
                if dumped is not None:
                    error = (
                        f"anti-cheat: {dumped!r} was printed before the stimulus "
                        "asked for it (pre-printed output)"
                    )
                else:
                    error = (
                        "anti-cheat: the firmware exited before the first wait-serial "
                        "step - expected output must be produced in response to the "
                        "stimulus"
                    )
                error_kind = common.ERROR_RUN
            if exit_code not in (0, None):
                error = f"micropython exited with code {exit_code}"
                error_kind = common.ERROR_RUN
        finally:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
    except ValueError as e:
        error = f"failed to prepare task: {e}"
        error_kind = common.ERROR_INFRA
    except ConnectionError as e:
        error = f"failed to connect: {e}"
        error_kind = common.ERROR_INFRA
    except FileNotFoundError as e:
        error = f"not found: {e.filename or e}"
        error_kind = common.ERROR_INFRA
    except OSError as e:
        error = f"I/O error while starting micropython: {e}"
        error_kind = common.ERROR_INFRA
    finally:
        if proc is not None:
            common._reap(proc)
            if proc.stdout is not None:
                proc.stdout.close()
        if mqtt_client is not None:
            try:
                mqtt_client.close()
            except OSError:
                pass
        _stop_broker_proc(broker_proc, broker_pid)

    serial_log.write_text(serial_text, encoding="utf-8")
    duration = round(time.monotonic() - start, 2)
    missed, hit_fail = common._check_patterns(serial_text, task.expect, task.fail)
    missed = missed + _check_events(serial_text, task.events)
    # IH-24 anchor: the same period bounds re-checked over the REAL chunk
    # ingestion stamps - a fake-timestamp dump fails here even when its
    # embedded timestamps look right. Passive tasks (no trigger writes)
    # legitimately skip the anchor (no stimulus to be anchored to).
    if task.events and trigger_stamps:
        missed = missed + _check_events_realtime(box, task.events)
    passed = not missed and not hit_fail and error is None
    result = common.TaskResult(
        task=task.name,
        passed=passed,
        exit_code=exit_code,
        duration_sec=duration,
        serial_log=serial_log if serial_text else None,
        missed=missed,
        hit_fail=hit_fail,
        error=error,
        error_kind=error_kind,
    )
    common._journal_result(journal, result)
    return result
