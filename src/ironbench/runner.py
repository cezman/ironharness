"""ironbench task runner: targets wokwi (cloud), renode (WSL2), unix (local),
plant (a pure-Python loop) and real (a live board, REPL over SerialTransport -
see realhw.py). serial log -> pattern scoring.

Scoring belongs to the runner (not the Wokwi scenario): after every run the
serial log is re-checked against the expect/fail patterns from task.yaml.

MicroPython under wokwi-cli does not auto-run main.py (bare firmware + REPL),
so when a task has no static scenario the runner generates a REPL-paste
scenario: wait for the '>>>' prompt, paste the entry file code in raw-paste
mode (Ctrl+E/Ctrl+D) and wait for the expected lines. For golden tasks the
entry is solution.py (the reference); main.py is the file the benchmark agent
writes.

wokwi-cli exit codes: 0 - the scenario finished, 42 - the --timeout fired.
For firmware with an infinite loop 42 is normal, so both codes count as
success when all patterns match. Runs are journaled via io_core.JsonlJournal.

Renode target: MicroPython runs in Renode under WSL2 (litex_vexriscv, the ELF
lives in tasks/_firmware). The UART is attached to a Renode TCP terminal; the
runner itself speaks the same REPL protocol over the socket (nudge -> Ctrl+E
paste mode -> code -> Ctrl+D -> stimulus) and writes the serial log. Tasks
declare the target with the renode section in task.yaml
(platform/firmware/uart). From Windows the Renode port is reachable directly
(WSL2 localhost-forwarding). Stage files travel into WSL as a tar stream over
stdin, because drvfs automounting is disabled in the distro.

Limitation of the pinned litex-ELF (v1.11, 2019): ~2 KB heap, no machine/time/
input/sys.stdin - only "print without input" tasks run under it. GPIO tasks
stay on wokwi; a fresh MicroPython build for litex is in the backlog (PLAN.md).

Plant target (see ironbench/plant.py): a closed "first-order plant +
controller" loop entirely in Python, no simulators or WSL. The worker runs the
controller (entry) in a separate process; scoring uses the step-response
metrics from task.yaml (missed = unmet requirements as human-readable strings).
"""

from __future__ import annotations

import dataclasses
import io
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import tomllib
from pathlib import Path

import yaml

from io_core.faults import FaultyTransport
from io_core.mqtt_transport import MqttTransport
from io_core.serial_transport import SerialTransport
from ironbench.realhw import RealRepl
from ironbench.tasks import Task

# wokwi-cli exit codes: a fired --timeout (42) is normal for infinite firmware
WOKWI_TIMEOUT_EXIT = 42
OK_EXIT_CODES = (0, WOKWI_TIMEOUT_EXIT)

# Wall-clock headroom on top of the sim timeout (cloud simulation
# startup). Module constant - tests patch it to avoid really waiting.
WALL_GRACE_SEC = 15

# MicroPython REPL prompt telling us the device is ready for pasting
REPL_PROMPT = ">>>"

# Shared directory of pinned firmware: do not duplicate a bin into the task
# directory; the runner stages it per the elf/firmware links in wokwi.toml
FIRMWARE_DIR = Path(__file__).resolve().parent / "tasks" / "_firmware"

# --- renode target ---

# Default socket-terminal port: in WSL mode it is only a placeholder in
# the .resc - the pipeline substitutes a free port (RENODE_PORT=...) and the
# runner connects to it; in test mode (command injection) the port comes
# from IRONBENCH_RENODE_PORT
RENODE_PORT = 3456

# Wall-clock deadlines: Renode+Mono startup takes seconds, paste mode answers
# at once. Module constants - tests patch them to avoid really waiting.
RENODE_CONNECT_SEC = 20
RENODE_STEP_SEC = 10

# Where the task stage lands in WSL2 (drvfs automount is disabled in the distro)
RENODE_REMOTE_ROOT = "$HOME/ironharness-runs"


def load_env_file(path: Path) -> dict[str, str]:
    """Parses .env: KEY=VALUE / export KEY=VALUE / $env:KEY='VALUE' (owner's style)."""
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        ps = re.match(r"^\$env:([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        exported = re.match(r"^export\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        plain = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        m = ps or exported or plain
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        env[key] = value
    return env


def find_env_file() -> Path | None:
    """First existing .env: in cwd or at the repository root (two levels above src/)."""
    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"]
    return next((p for p in candidates if p.is_file()), None)


def resolve_token(explicit: str | None = None) -> str | None:
    """Wokwi token: explicit argument > WOKWI_CLI_TOKEN env var > .env."""
    if explicit:
        return explicit
    if os.environ.get("WOKWI_CLI_TOKEN"):
        return os.environ["WOKWI_CLI_TOKEN"]
    env_file = find_env_file()
    if env_file:
        return load_env_file(env_file).get("WOKWI_CLI_TOKEN")
    return None


def default_cli() -> str:
    """wokwi-cli path: on PATH or the installer default location (~/.wokwi/bin)."""
    found = shutil.which("wokwi-cli")
    if found:
        return found
    exe = "wokwi-cli.exe" if sys.platform == "win32" else "wokwi-cli"
    candidate = Path.home() / ".wokwi" / "bin" / exe
    return str(candidate) if candidate.is_file() else "wokwi-cli"


def _plain_text(pattern: str) -> str | None:
    """A pattern without regex metacharacters also works as wait-serial (early finish)."""
    return pattern if not re.search(r"[\\^$.|?*+()\[\]{}]", pattern) else None


def generate_paste_scenario(task: Task) -> str:
    """Scenario YAML: paste the entry file code into the REPL, run the stimulus, wait for expect."""
    code = (task.directory / task.entry).read_text(encoding="utf-8")
    steps: list[dict[str, object]] = [
        {"wait-serial": REPL_PROMPT},
        {"write-serial": "\x05"},  # Ctrl+E: raw-paste mode
        {"delay": "200ms"},
        {"write-serial": code},
        {"write-serial": "\x04"},  # Ctrl+D: execute
        *task.stimulus,  # interaction with the firmware (serial input, buttons, sensors)
    ]
# wait-serial on literal expect patterns: the scenario finishes the
    # simulation early once everything expected has been printed (saves Wokwi quota)
    for pattern in task.expect:
        plain = _plain_text(pattern)
        if plain:
            steps.append({"wait-serial": plain})
    doc = {
        "name": f"{task.name}-paste",
        "version": 1,
        "author": "ironbench",
        "steps": steps,
    }
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)


@dataclasses.dataclass(frozen=True)
class TaskResult:
    """Result of a single task run."""

    task: str
    passed: bool
    exit_code: int | None
    duration_sec: float
    serial_log: Path | None
    missed: tuple[str, ...] = ()
    hit_fail: tuple[str, ...] = ()
    error: str | None = None


def _check_patterns(
    serial_text: str, expect: tuple[str, ...], fail: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    missed = tuple(p for p in expect if not re.search(p, serial_text))
    hit_fail = tuple(p for p in fail if re.search(p, serial_text))
    return missed, hit_fail


def _stage_task(task: Task, out_dir: Path) -> tuple[Path, str]:
    """Copies the task directory into a clean stage; returns (path, scenario name)."""
    stage = out_dir / task.name
    shutil.rmtree(stage, ignore_errors=True)  # without this, stale files survive the run
    stage.mkdir(parents=True, exist_ok=True)
    for item in task.directory.iterdir():
        if item.is_file():
            shutil.copy2(item, stage / item.name)
    _stage_firmware(task, stage)
    if task.scenario:
        return stage, task.scenario
    scenario_file = stage / "generated.scenario.yaml"
    scenario_file.write_text(generate_paste_scenario(task), encoding="utf-8")
    return stage, scenario_file.name


def _stage_firmware(task: Task, stage: Path) -> None:
    """Adds elf/firmware from the shared FIRMWARE_DIR when the task does not ship them."""
    wokwi_toml = stage / "wokwi.toml"
    if not wokwi_toml.is_file():
        return
    config = tomllib.loads(wokwi_toml.read_text(encoding="utf-8")).get("wokwi", {})
    for key in ("elf", "firmware"):
        name = config.get(key)
        if isinstance(name, str) and not (stage / name).is_file():
            shared = FIRMWARE_DIR / name
            if shared.is_file():
                shutil.copy2(shared, stage / name)
            else:
                raise ValueError(
                    f"firmware {name!r} not found in the task directory or in tasks/_firmware/"
                )


def _journal_result(journal, result: TaskResult) -> None:
    if journal:
        journal(
            "task_result",
            {
                "task": result.task,
                "passed": result.passed,
                "exit_code": result.exit_code,
                "duration_sec": result.duration_sec,
                "missed": list(result.missed),
                "hit_fail": list(result.hit_fail),
                "error": result.error,
            },
        )


def run_task(
    task: Task,
    *,
    out_dir: Path,
    cli_path: str | None = None,
    token: str | None = None,
    renode_cmd: str | list | None = None,
    unix_cmd: str | list | None = None,
    plant_cmd: str | list | None = None,
    mqtt_broker=None,
    journal=None,
    real_transport=None,
    real_port: str | None = None,
) -> TaskResult:
    """Target dispatcher: wokwi/renode/unix/plant/real are implemented.

    cli_path/token/renode_cmd/unix_cmd/plant_cmd are injection points for tests
    (fake CLIs instead of real ones). mqtt_broker - an already-running
    MqttSimBroker for tests (by default the task broker starts in WSL2).
    journal - io_core.JsonlJournal: we write task_start/task_result. real:
    real_transport - a ready transport (tests, usually loop://), real_port -
    the live board's COM port (otherwise env IRONBENCH_REAL_PORT).
    """
    if task.target == "wokwi":
        return _run_wokwi(task, out_dir=out_dir, cli_path=cli_path, token=token, journal=journal)
    if task.target == "renode":
        return _run_renode(task, out_dir=out_dir, renode_cmd=renode_cmd, journal=journal)
    if task.target == "unix":
        return _run_unix(
            task, out_dir=out_dir, unix_cmd=unix_cmd, mqtt_broker=mqtt_broker, journal=journal
        )
    if task.target == "plant":
        return _run_plant(task, out_dir=out_dir, plant_cmd=plant_cmd, journal=journal)
    if task.target == "real":
        return _run_real(
            task, out_dir=out_dir, transport=real_transport, port=real_port, journal=journal
        )
    result = TaskResult(
        task=task.name,
        passed=False,
        exit_code=None,
        duration_sec=0.0,
        serial_log=None,
        missed=tuple(task.expect),
        error=f"unknown target {task.target!r}; checks were not run",
    )
    _journal_result(journal, result)
    return result


def is_infra_error(error: str | None) -> bool:
    """An environment-level infrastructure failure (the agent cannot fix it) -
    used for an early exit from the solve loop, so LLM iterations are not
    burned on a hopeless error."""
    if not error:
        return False
    marks = (
        "not implemented",
        "not supported",
        "not found",
        "failed to prepare task",
        "failed to connect",
        "not responding",
        "paste mode",
    )
    return any(m in error for m in marks)


def _run_wokwi(
    task: Task,
    *,
    out_dir: Path,
    cli_path: str | None = None,
    token: str | None = None,
    journal=None,
) -> TaskResult:
    """Runs the task in Wokwi and returns the result (pass/fail + reason)."""
    cli = cli_path or default_cli()
    cli_cmd = [cli] if isinstance(cli, str) else list(cli)  # tests pass a command list
    out_dir.mkdir(parents=True, exist_ok=True)
    serial_log = out_dir / f"{task.name}.serial.log"
    wall_timeout = task.timeout_sec * 2 + WALL_GRACE_SEC

    token = resolve_token(token)
    env = {**os.environ}
    if token:
        env["WOKWI_CLI_TOKEN"] = token

    start = time.monotonic()
    exit_code: int | None = None
    error: str | None = None
    stage = scenario_name = None
    try:
        stage, scenario_name = _stage_task(task, out_dir)
    except (OSError, ValueError) as e:
        # missing entry/firmware, broken wokwi.toml - a clean FAIL instead of a crash
        error = f"failed to prepare task: {e}"
    if stage is not None:
        cmd = [
            *cli_cmd,
            str(stage),
            "--scenario",
            scenario_name,
            "--timeout",
            str(task.timeout_sec * 1000),
            "--serial-log-file",
            str(serial_log),
            "--timeout-exit-code",
            str(WOKWI_TIMEOUT_EXIT),
            "-q",
        ]
        if journal:
            journal("task_start", {"task": task.name})
        try:
            proc = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=wall_timeout,
                check=False,
            )
            exit_code = proc.returncode
            if proc.returncode not in OK_EXIT_CODES:
                error = (proc.stderr or proc.stdout or "").strip()[-500:] or None
        except subprocess.TimeoutExpired:
            error = f"runner wall-clock timeout ({wall_timeout} s)"
        except FileNotFoundError:
            error = f"wokwi-cli not found: {cli}"

    duration = round(time.monotonic() - start, 2)
    serial_text = (
        serial_log.read_text(encoding="utf-8", errors="replace") if serial_log.is_file() else ""
    )
    missed, hit_fail = _check_patterns(serial_text, task.expect, task.fail)
    passed = exit_code in OK_EXIT_CODES and not missed and not hit_fail and error is None
    result = TaskResult(
        task=task.name,
        passed=passed,
        exit_code=exit_code,
        duration_sec=duration,
        serial_log=serial_log if serial_text else None,
        missed=missed,
        hit_fail=hit_fail,
        error=error,
    )
    _journal_result(journal, result)
    return result


# --- renode target: Renode in WSL2, UART over a TCP terminal ---


def _parse_delay(value: str) -> float:
    """'1500ms' -> 1.5, '2s' -> 2.0; no suffix means seconds."""
    text = str(value).strip().lower()
    if text.endswith("ms"):
        return int(text[:-2]) / 1000
    if text.endswith("s"):
        return float(text[:-1])
    return float(text)


def generate_renode_resc(task: Task, port: int) -> str:
    """Renode script: platform from the Renode shipset, UART on a TCP terminal,
    firmware from __FIRMWARE__.

    __FIRMWARE__ is substituted by sed inside WSL with the absolute stage path
    (Python does not know the distro's $HOME); the @ path marker stays in the
    template - sed used to swallow it together with the @FIRMWARE@ placeholder,
    so the monitor got a path without @. The UART peripheral name comes from
    the renode section.
    """
    uart = task.renode.get("uart", "uart")
    return f""":name: ironbench {task.name}
using sysbus
mach create
machine LoadPlatformDescription @platforms/cpus/{task.renode['platform']}.repl
emulation CreateServerSocketTerminal {port} "term"
connector Connect sysbus.{uart} term
sysbus LoadELF @__FIRMWARE__
start
"""


def _stage_renode_task(task: Task, out_dir: Path, port: int) -> Path:
    """Task stage for renode: .resc and wsl-run.sh (they travel into WSL); local
    copies stay behind for debugging. The firmware ships separately
    (_push_firmware): large blobs arrive corrupted through the wsl.exe stdin relay."""
    if not task.renode.get("platform") or not task.renode.get("firmware"):
        raise ValueError("the renode section requires platform and firmware")
    stage = out_dir / task.name
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    entry = task.directory / task.entry
    if not entry.is_file():
        raise ValueError(f"entry file not found: {entry}")
    firmware = task.renode["firmware"]
    firmware_src = task.directory / firmware
    if not firmware_src.is_file():
        firmware_src = FIRMWARE_DIR / firmware
    if not firmware_src.is_file():
        raise ValueError(f"firmware {firmware!r} not found in the task directory or in tasks/_firmware/")
    (stage / "renode.resc").write_text(
        generate_renode_resc(task, port), encoding="utf-8", newline="\n"
    )
    (stage / "wsl-run.sh").write_text(_wsl_run_script(task), encoding="utf-8", newline="\n")
    return stage


def _wsl_run_script(task: Task) -> str:
    """wsl-run.sh: all the Renode startup logic inside WSL. It lives as a file
    (travels in the tiny stage tar), so it does not depend on wsl.exe argv
    quirks. The port is a free one: the previous run's port may be held by a
    zombie listener of the WSL2 relay. pkill -9 is required: mono takes over a
    second to die from SIGTERM and keeps the port; the pattern matches binary
    paths (symlink and portable) but not our renode-* directories/files."""
    renode_bin = os.environ.get("IRONBENCH_RENODE_BIN", "~/renode/renode")
    firmware = task.renode["firmware"]
    script = f"""#!/bin/bash
cd "$(dirname "$0")"
pkill -9 -f 'renode/renode|renode_[0-9]' 2>/dev/null
for i in $(seq 20); do pgrep -f 'renode/renode|renode_[0-9]' >/dev/null 2>&1 || break; sleep 0.5; done
PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('0.0.0.0',0)); print(s.getsockname()[1])")
sed -i "s|__FIRMWARE__|$HOME/ironharness-firmware/{firmware}|; s/CreateServerSocketTerminal [0-9][0-9]*/CreateServerSocketTerminal $PORT/" renode.resc
echo "RENODE_PORT=$PORT"
sleep infinity | {renode_bin} --disable-xwt --console renode.resc > run.log 2>&1
"""
    return script


def _wsl_distro() -> str:
    return os.environ.get("IRONBENCH_RENODE_DISTRO", "OpenClawGateway")


def _tar_of(path: Path, arcname: str) -> bytes:
    """One file as a tar.gz blob (to pass into WSL stdin via communicate)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(path, arcname=arcname)
    return buf.getvalue()


def _push_to_wsl(blob: bytes, remote_dir: str, marker: str) -> None:
    """tar.gz blob into WSL stdin (via communicate: the wsl.exe relay is only
    reliable that way); the marker in stdout confirms the extraction."""
    try:
        proc = subprocess.run(
            [
                "wsl",
                "-d",
                _wsl_distro(),
                "--",
                "bash",
                "-c",
                f"mkdir -p {remote_dir} && tar -xzf - -C {remote_dir} && echo {marker}",
            ],
            input=blob,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ConnectionError("WSL did not respond during file transfer (120 s timeout)") from None
    if marker not in proc.stdout.decode("utf-8", "replace"):
        raise ConnectionError(
            f"failed to push files into WSL ({remote_dir}): "
            + (proc.stdout + proc.stderr).decode("utf-8", "replace")[-300:]
        )


def _push_firmware(task: Task) -> None:
    """Pushes the firmware into the persistent WSL directory
    ~/ironharness-firmware, where wsl-run.sh reads it (sed substitutes the path
    into the .resc)."""
    firmware = task.renode["firmware"]
    src = task.directory / firmware
    if not src.is_file():
        src = FIRMWARE_DIR / firmware
    if not src.is_file():
        raise ValueError(f"firmware {firmware!r} not found in the task directory or in tasks/_firmware/")
    _push_to_wsl(_tar_of(src, src.name), "$HOME/ironharness-firmware", "FW-PUSHED")


def _wsl_renode_cmd(remote_dir: str) -> list[str]:
    """Runs wsl-run.sh from the stage; stdin stays free (DEVNULL), logs go to stdout."""
    return ["wsl", "-d", _wsl_distro(), "--", "bash", "-c", f"bash {remote_dir}/wsl-run.sh"]


class _TelnetFilter:
    """Strips telnet IAC sequences: the Renode socket terminal is a telnet
    server, it sends negotiation (IAC WILL/DO...) and escapes 0xFF bytes in
    data. An incomplete IAC sequence at a chunk boundary waits for the rest in
    the next chunk (the buffer tail persists between feed calls)."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._iac = False  # waiting for the command byte after IAC
        self._sub = False  # inside IAC SB ... IAC SE

    def feed(self, data: bytes) -> str:
        self._buf += data
        out = bytearray()
        i = 0
        n = len(self._buf)
        while i < n:
            b = self._buf[i]
            if self._sub:
                if b == 0xFF:
                    if i + 1 >= n:  # incomplete: wait for the rest
                        break
                    if self._buf[i + 1] == 0xF0:  # IAC SE - end of subnegotiation
                        self._sub = False
                    i += 2  # IAC SE, an escaped 0xFF, or junk inside SB
                else:
                    i += 1
            elif self._iac:
                if b in (0xFB, 0xFC, 0xFD, 0xFE):  # WILL/WONT/DO/DONT + option byte
                    if i + 1 >= n:  # option byte not there yet - wait for the rest
                        break
                    i += 2
                    self._iac = False
                elif b == 0xFA:  # SB: subnegotiation until IAC SE
                    self._sub = True
                    i += 1
                    self._iac = False
                elif b == 0xFF:  # escaped literal 0xFF
                    out.append(0xFF)
                    i += 1
                    self._iac = False
                else:  # NOP/GA and other argument-less commands
                    i += 1
                    self._iac = False
            elif b == 0xFF:
                self._iac = True
                i += 1
            else:
                out.append(b)
                i += 1
        del self._buf[:i]
        return out.decode("utf-8", "replace")


def _recv_until(
    sock: socket.socket,
    needles: tuple[str, ...],
    deadline: float,
    tel: _TelnetFilter,
) -> tuple[str, bool]:
    """Reads the socket (stripping telnet IAC) until one of the needles
    appears or the deadline expires."""
    buf = ""
    while time.monotonic() < deadline:
        sock.settimeout(max(0.05, min(0.2, deadline - time.monotonic())))
        try:
            data = sock.recv(4096)
        except TimeoutError:
            continue
        except OSError:
            break
        if not data:
            break
        buf += tel.feed(data)
        if any(n in buf for n in needles):
            return buf, True
    return buf, False


def _paste_code(code: str) -> str:
    """Drops whole-line comments: paste mode sends the source as-is and the
    emulator's UART FIFO is finite - every extra byte raises the risk of a
    truncated paste. Code lines stay untouched (trailing comments remain)."""
    return "\n".join(line for line in code.splitlines() if not line.lstrip().startswith("#"))
def _send_chunked(sock: socket.socket, data: bytes, chunk: int = 32, pause: float = 0.05) -> None:
    """Pasting in chunks: legacy paste mode has no flow control, the emulator
    FIFO overflows when poured in a single piece."""
    for i in range(0, len(data), chunk):
        sock.sendall(data[i : i + chunk])
        time.sleep(pause)


def _drive_repl(sock: socket.socket, task: Task, wall_deadline: float) -> tuple[str, str | None]:
    """Talks to the MicroPython REPL over the socket: paste the entry code, run
    the stimulus, collect the serial output.

    Returns (serial text, error|None). The protocol mirrors the wokwi paste
    scenario, but raw-paste is not supported by the litex firmware - legacy
    paste mode is used (Ctrl+E, the answer 'paste mode; Ctrl-C to cancel...').
    """
    parts: list[str] = []
    tel = _TelnetFilter()
    # nudge the REPL with an empty line: the prompt prints once and is easy to miss on connect
    sock.sendall(b"\n")
    buf, ok = _recv_until(sock, (REPL_PROMPT,), wall_deadline, tel)
    parts.append(buf)
    if not ok:
        return "".join(parts), "REPL is not responding (no '>>>' prompt)"

    sock.sendall(b"\x05")
    buf, ok = _recv_until(
        sock, ("paste mode",), min(wall_deadline, time.monotonic() + RENODE_STEP_SEC), tel
    )
    parts.append(buf)
    if not ok:
        return "".join(parts), "paste mode unavailable (firmware without Ctrl+E)"

    code = (task.directory / task.entry).read_text(encoding="utf-8")
    _send_chunked(sock, _paste_code(code).encode("utf-8") + b"\n\x04")

    # stimulus steps; set-control (Wokwi buttons) cannot be reproduced under renode
    for step in task.stimulus:
        if time.monotonic() > wall_deadline:
            break
        if "set-control" in step:
            return "".join(parts), f"set-control step not supported by the renode target: {step}"
        if "delay" in step:
            time.sleep(min(_parse_delay(step["delay"]), max(0.0, wall_deadline - time.monotonic())))
        elif "write-serial" in step:
            sock.sendall(str(step["write-serial"]).encode("utf-8"))
        elif "wait-serial" in step:
            buf, _ = _recv_until(sock, (str(step["wait-serial"]),), wall_deadline, tel)
            parts.append(buf)

    # keep reading: until the full set of literal expects is collected (early
    # exit, like wait-serial in the wokwi scenario), until the prompt returns
    # (a finite program finished), or until the deadline (infinite firmware loop)
    plain = tuple(p for p in task.expect if _plain_text(p))
    buf = ""
    while time.monotonic() < wall_deadline:
        sock.settimeout(max(0.05, min(0.5, wall_deadline - time.monotonic())))
        try:
            data = sock.recv(4096)
        except TimeoutError:
            continue
        except OSError:
            break
        if not data:
            break
        buf += tel.feed(data)
        if (plain and all(p in buf for p in plain)) or REPL_PROMPT in buf:
            break
    parts.append(buf)
    return "".join(parts), None


def _read_marker_line(stream, marker: str, timeout: float) -> str:
    """Reads the pipeline stdout until a '<marker>=<value>' line (in a separate
    thread: readline blocks, and the pipeline may die before echoing). Lines
    before the marker (e.g. wsl.exe service messages in stderr, merged into
    stdout) are skipped. Returns the value after '='."""
    box: dict[str, str] = {}

    def reader():
        try:
            for line in iter(stream.readline, b""):
                if line.startswith(marker.encode() + b"="):
                    raw = line.decode("utf-8", "replace").strip().split("=", 1)[1]
                    if raw:  # junk after '=' - keep reading
                        box["value"] = raw
                        return
        except (OSError, ValueError):
            pass

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    thread.join(timeout)
    if "value" not in box:
        raise ConnectionError(f"WSL pipeline did not report {marker}=...")
    return box["value"]


def _read_port_line(stream, timeout: float) -> int:
    """Renode socket-terminal port from the WSL pipeline stdout."""
    raw = _read_marker_line(stream, "RENODE_PORT", timeout)
    if not raw.isdigit():  # junk after '=' - nothing more to read, fail honestly
        raise ConnectionError(f"WSL pipeline reported a non-numeric port: {raw!r}")
    return int(raw)


def _run_renode(
    task: Task,
    *,
    out_dir: Path,
    renode_cmd: str | list | None = None,
    journal=None,
) -> TaskResult:
    """Runs the task in Renode and returns the result (pass/fail + reason).

    renode_cmd=None -> the standard WSL path (firmware and stage go as tar
    blobs via communicate, then wsl-run.sh starts and reports the port);
    command injection (tests) runs the fake "Renode" locally, without WSL.
    """
    if "set-control" in {k for step in task.stimulus for k in step}:
        result = TaskResult(
            task=task.name,
            passed=False,
            exit_code=None,
            duration_sec=0.0,
            serial_log=None,
            missed=tuple(task.expect),
            error="set-control is not supported by the renode target (Wokwi buttons)",
        )
        _journal_result(journal, result)
        return result

    out_dir.mkdir(parents=True, exist_ok=True)
    serial_log = out_dir / f"{task.name}.serial.log"
    port = int(os.environ.get("IRONBENCH_RENODE_PORT", RENODE_PORT))
    wall_timeout = task.timeout_sec * 2 + WALL_GRACE_SEC
    start = time.monotonic()
    exit_code: int | None = None
    error: str | None = None
    serial_text = ""
    proc = None
    try:
        stage = _stage_renode_task(task, out_dir, port)
        if renode_cmd is None:
            _push_firmware(task)
            _push_to_wsl(
                _tar_of_files(stage), f"{RENODE_REMOTE_ROOT}/{task.name}", "STAGE-PUSHED"
            )
            cmd = _wsl_renode_cmd(f"{RENODE_REMOTE_ROOT}/{task.name}")
        else:
            cmd = [renode_cmd] if isinstance(renode_cmd, str) else list(renode_cmd)
        if journal:
            journal("task_start", {"task": task.name})
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if renode_cmd is None:
            # wsl-run.sh picks a free port itself and reports it
            port = _read_port_line(proc.stdout, timeout=RENODE_CONNECT_SEC)
        # wait for the TCP terminal: Renode+Mono startup in WSL takes seconds
        sock = None
        connect_deadline = time.monotonic() + RENODE_CONNECT_SEC
        while sock is None:
            try:
                sock = socket.create_connection(("localhost", port), timeout=1.0)
            except OSError:
                if proc.poll() is not None or time.monotonic() > connect_deadline:
                    raise ConnectionError(
                        f"Renode socket terminal on :{port} is unreachable"
                    ) from None
                time.sleep(0.2)
        with sock:
            serial_text, error = _drive_repl(sock, task, time.monotonic() + wall_timeout)
        exit_code = 0 if error is None else None
    except ValueError as e:
        error = f"failed to prepare task: {e}"
    except ConnectionError as e:
        error = f"failed to connect: {e}"
    except FileNotFoundError as e:
        error = f"not found: {e.filename or e}"
    except OSError as e:
        error = f"I/O error while starting Renode: {e}"
    finally:
        if proc is not None:
            _reap(proc)
            if proc.stdout is not None:
                proc.stdout.close()

    serial_log.write_text(serial_text, encoding="utf-8")
    duration = round(time.monotonic() - start, 2)
    missed, hit_fail = _check_patterns(serial_text, task.expect, task.fail)
    passed = not missed and not hit_fail and error is None
    result = TaskResult(
        task=task.name,
        passed=passed,
        exit_code=exit_code,
        duration_sec=duration,
        serial_log=serial_log if serial_text else None,
        missed=missed,
        hit_fail=hit_fail,
        error=error,
    )
    _journal_result(journal, result)
    return result


def _tar_of_files(stage: Path) -> bytes:
    """All stage files as one tar.gz blob (tiny: resc + wsl-run.sh)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for item in sorted(stage.iterdir()):
            tar.add(item, arcname=item.name)
    return buf.getvalue()


# --- unix target: MicroPython unix port in WSL2 (free local runs) ---


def _unix_cmd(remote_entry: str, env_prefix: str = "") -> list[str]:
    """micropython executes the entry directly: stdin = stimulus, stdout = serial log."""
    upy_bin = os.environ.get("IRONBENCH_UNIX_BIN", "~/bin/micropython")
    return [
        "wsl",
        "-d",
        _wsl_distro(),
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


# --- MQTT on the unix target: mqtt_sim broker next to the firmware (see io_core/mqtt_sim.py) ---

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
    remote_dir = f"{RENODE_REMOTE_ROOT}/{task.name}-mqtt"
    _push_to_wsl(_tar_of(MQTT_SIM_PATH, "mqtt_sim.py"), remote_dir, "BROKER-PUSHED")
    proc = subprocess.Popen(
        [
            "wsl",
            "-d",
            _wsl_distro(),
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
    deadline = time.monotonic() + RENODE_CONNECT_SEC
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
        wsl_pid = int(_read_marker_line(proc.stdout, "BROKER_PID", 5))
    except (ConnectionError, ValueError):
        wsl_pid = None  # without a PID, cleanup degrades to terminating wsl.exe
    return proc, wsl_pid


def _reap(proc: subprocess.Popen) -> None:
    """Shuts down the local wsl.exe/worker; a second wait after kill raises no exception."""
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _stop_broker_proc(proc: subprocess.Popen | None, wsl_pid: int | None) -> None:
    if proc is None:
        return
    if wsl_pid is not None:
        try:
            subprocess.run(
                ["wsl", "-d", _wsl_distro(), "--", "kill", str(wsl_pid)],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    _reap(proc)
    if proc.stdout is not None:
        proc.stdout.close()


def _run_unix(
    task: Task,
    *,
    out_dir: Path,
    unix_cmd: str | list | None = None,
    mqtt_broker=None,
    journal=None,
) -> TaskResult:
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
        result = TaskResult(
            task=task.name,
            passed=False,
            exit_code=None,
            duration_sec=0.0,
            serial_log=None,
            missed=tuple(task.expect),
            error="set-control is not supported by the unix target (Wokwi buttons/sensors)",
        )
        _journal_result(journal, result)
        return result

    out_dir.mkdir(parents=True, exist_ok=True)
    serial_log = out_dir / f"{task.name}.serial.log"
    wall_timeout = task.timeout_sec * 2 + WALL_GRACE_SEC
    start = time.monotonic()
    exit_code: int | None = None
    error: str | None = None
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
            remote_dir = f"{RENODE_REMOTE_ROOT}/{task.name}-unix"
            _push_to_wsl(
                _tar_of(task.directory / task.entry, task.entry), remote_dir, "STAGE-PUSHED"
            )
            env_prefix = (
                f"IRONBENCH_MQTT_HOST=127.0.0.1 IRONBENCH_MQTT_PORT={mqtt_port} "
                if task.mqtt
                else ""
            )
            cmd = _unix_cmd(f"{remote_dir}/{task.entry}", env_prefix)
        else:
            cmd = [unix_cmd] if isinstance(unix_cmd, str) else list(unix_cmd)
        if journal:
            journal("task_start", {"task": task.name})
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # the unix port writes tracebacks to stderr
        )
        box = {"text": "", "eof": False}

        def reader():
            try:
                for line in iter(proc.stdout.readline, b""):
                    box["text"] += line.decode("utf-8", "replace")
            except OSError:
                pass
            finally:
                box["eof"] = True

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()
        deadline = time.monotonic() + wall_timeout
        plain = tuple(p for p in task.expect if _plain_text(p))
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
        try:
            for step in task.stimulus:
                if time.monotonic() > deadline or box["eof"]:
                    break
                if "delay" in step:
                    time.sleep(
                        min(_parse_delay(step["delay"]), max(0.0, deadline - time.monotonic()))
                    )
                elif "wait-serial" in step:
                    needle = str(step["wait-serial"])
                    while (
                        needle not in box["text"]
                        and time.monotonic() < deadline
                        and not box["eof"]
                    ):
                        time.sleep(0.05)
                elif "write-serial" in step:
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
                            box["text"] += f"mqtt: {msg['topic']} {msg['payload']}\n"
                    if got < need:
                        box["text"] += (
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
            if exit_code not in (0, None):
                error = f"micropython exited with code {exit_code}"
        finally:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
    except ValueError as e:
        error = f"failed to prepare task: {e}"
    except ConnectionError as e:
        error = f"failed to connect: {e}"
    except FileNotFoundError as e:
        error = f"not found: {e.filename or e}"
    except OSError as e:
        error = f"I/O error while starting micropython: {e}"
    finally:
        if proc is not None:
            _reap(proc)
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
    missed, hit_fail = _check_patterns(serial_text, task.expect, task.fail)
    passed = not missed and not hit_fail and error is None
    result = TaskResult(
        task=task.name,
        passed=passed,
        exit_code=exit_code,
        duration_sec=duration,
        serial_log=serial_log if serial_text else None,
        missed=missed,
        hit_fail=hit_fail,
        error=error,
    )
    _journal_result(journal, result)
    return result


def _run_real(
    task: Task,
    *,
    out_dir: Path,
    transport=None,
    port: str | None = None,
    journal=None,
) -> TaskResult:
    """Live board: MicroPython REPL over SerialTransport (see ironbench/realhw.py).

    On an ESP32 with a USB-UART bridge the REPL and the application UART share
    one line, so semantics follow the unix target: the entry is pasted into
    the REPL in raw-paste mode (Ctrl+E/Ctrl+D), the stimulus drives
    delay/write-serial/wait-serial, and expect/fail score the accumulated
    output. Port: real_transport (tests) -> real_port -> env
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
        result = TaskResult(
            task=task.name,
            passed=False,
            exit_code=None,
            duration_sec=0.0,
            serial_log=None,
            missed=tuple(task.expect),
            error=unsupported,
        )
        _journal_result(journal, result)
        return result

    if transport is None:
        port = port or os.environ.get("IRONBENCH_REAL_PORT")
        if not port:
            result = TaskResult(
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
            )
            _journal_result(journal, result)
            return result

        def transport_factory():
            t = SerialTransport(port, baudrate=115200, timeout=0.5)
            t.open()
            return t
    else:
        def transport_factory():
            return transport  # test injection of a fake

    out_dir.mkdir(parents=True, exist_ok=True)
    serial_log = out_dir / f"{task.name}.serial.log"
    wall_timeout = task.timeout_sec * 2 + WALL_GRACE_SEC
    start = time.monotonic()
    error: str | None = None
    serial_text = ""
    repl: RealRepl | None = None
    try:
        code = (task.directory / task.entry).read_text(encoding="utf-8")
        if journal:
            journal("task_start", {"task": task.name})
        repl = RealRepl(transport_factory())
        deadline = time.monotonic() + wall_timeout
        repl.boot(code)
        plain = tuple(p for p in task.expect if _plain_text(p))
        for step in task.stimulus:
            if time.monotonic() > deadline:
                break
            if "delay" in step:
                time.sleep(min(_parse_delay(step["delay"]), max(0.0, deadline - time.monotonic())))
            elif "wait-serial" in step:
                repl.wait_for(str(step["wait-serial"]), deadline)
            elif "write-serial" in step:
                # the REPL line editor ends input() on \r (\n is silent) -
                # normalize to \r, mirroring the unix target where \n is needed
                raw = str(step["write-serial"]).replace("\r\n", "\r").replace("\n", "\r")
                repl.write(raw.encode("utf-8"))
        # keep reading: until all literal expects are collected or the deadline
        # (an infinite firmware loop is normal)
        while time.monotonic() < deadline:
            if plain and all(p in repl.output() for p in plain):
                break
            time.sleep(0.05)
        serial_text = repl.output()
    except (OSError, ConnectionError, ValueError, AssertionError) as e:
        error = f"failed to talk to the board: {e}"
    finally:
        if repl is not None:
            repl.close()

    serial_log.write_text(serial_text, encoding="utf-8")
    duration = round(time.monotonic() - start, 2)
    missed, hit_fail = _check_patterns(serial_text, task.expect, task.fail)
    passed = not missed and not hit_fail and error is None
    result = TaskResult(
        task=task.name,
        passed=passed,
        exit_code=None,
        duration_sec=duration,
        serial_log=serial_log if serial_text else None,
        missed=missed,
        hit_fail=hit_fail,
        error=error,
    )
    _journal_result(journal, result)
    return result


# --- plant target: a closed plant + controller loop (see ironbench/plant.py) ---


def _run_plant(
    task: Task,
    *,
    out_dir: Path,
    plant_cmd: str | list | None = None,
    journal=None,
) -> TaskResult:
    """A run through the `python -m ironbench.plant` worker: it executes the
    controller (entry) in a separate process and writes the log + result.json.
    A hung/crashed controller is a run result (feedback to the agent), not a
    runner crash. plant_cmd is the injection point for tests. The target is
    local: no WSL, simulators, or their quotas needed."""
    out_dir.mkdir(parents=True, exist_ok=True)
    serial_log = out_dir / f"{task.name}.serial.log"
    result_file = out_dir / f"{task.name}.plant-result.json"
    wall_timeout = task.timeout_sec * 2 + WALL_GRACE_SEC
    start = time.monotonic()
    exit_code: int | None = None
    error: str | None = None
    report: dict | None = None
    try:
        entry = task.directory / task.entry
        if not entry.is_file():
            raise FileNotFoundError(f"entry file not found: {entry}")
        spec_file = out_dir / f"{task.name}.plant.json"
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
        )
        exit_code = proc.returncode
        if proc.returncode != 0:
            # the worker returns 0 even when the controller crashed - a non-zero
            # code means a problem in the worker/spec itself (not the agent's fault)
            error = (
                f"plant worker exited with code {proc.returncode}: "
                + (proc.stderr or proc.stdout or "").strip()[-300:]
            )
    except subprocess.TimeoutExpired:
        error = f"the plant run exceeded the wall limit ({wall_timeout} s) - controller stuck in a loop?"
    except FileNotFoundError as e:
        error = f"not found: {e.filename or e}"

    if error is None:
        if result_file.is_file():
            try:
                report = json.loads(result_file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                error = f"failed to parse the plant worker result.json: {e}"
        else:
            error = "the plant worker left no result.json"
    if report and report.get("error"):
        # error carries only the headline: the full traceback stays in the log
        # (feedback to the agent), and the agent's exception text must not leak
        # into is_infra_error
        error = report["error"].splitlines()[0]

    serial_text = (
        serial_log.read_text(encoding="utf-8", errors="replace") if serial_log.is_file() else ""
    )
    missed_metrics = tuple(report.get("missed", ())) if report else ()
    missed_patterns, hit_fail = _check_patterns(serial_text, task.expect, task.fail)
    missed = missed_metrics + missed_patterns
    passed = not missed and not hit_fail and error is None
    result = TaskResult(
        task=task.name,
        passed=passed,
        exit_code=exit_code,
        duration_sec=round(time.monotonic() - start, 2),
        serial_log=serial_log if serial_text else None,
        missed=missed,
        hit_fail=hit_fail,
        error=error,
    )
    _journal_result(journal, result)
    return result
