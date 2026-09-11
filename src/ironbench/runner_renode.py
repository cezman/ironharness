"""renode target: MicroPython runs in Renode under WSL2 (litex_vexriscv, the
ELF lives in tasks/_firmware). The UART is attached to a Renode TCP terminal;
the runner itself speaks the same REPL protocol over the socket (nudge ->
Ctrl+E paste mode -> code -> Ctrl+D -> stimulus) and writes the serial log.
Tasks declare the target with the renode section in task.yaml
(platform/firmware/uart). From Windows the Renode port is reachable directly
(WSL2 localhost-forwarding). Stage files travel into WSL as a tar stream over
stdin, because drvfs automounting is disabled in the distro.

Limitation of the pinned litex-ELF (v1.11, 2019): ~2 KB heap, no machine/time/
input/sys.stdin - only "print without input" tasks run under it. GPIO tasks
stay on wokwi; a fresh MicroPython build for litex is in the backlog (PLAN.md).
"""

from __future__ import annotations

import io
import os
import shutil
import socket
import subprocess
import tarfile
import time
from pathlib import Path

from ironbench import runner_common as common
from ironbench.tasks import Task

# Default socket-terminal port: in WSL mode it is only a placeholder in
# the .resc - the pipeline substitutes a free port (RENODE_PORT=...) and the
# runner connects to it; in test mode (command injection) the port comes
# from IRONBENCH_RENODE_PORT
RENODE_PORT = 3456

# Wall-clock deadlines: Renode+Mono startup takes seconds, paste mode answers
# at once. Module constants - tests patch them to avoid really waiting.
RENODE_CONNECT_SEC = 20
RENODE_STEP_SEC = 10


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
        firmware_src = common.FIRMWARE_DIR / firmware
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


def _push_firmware(task: Task) -> None:
    """Pushes the firmware into the persistent WSL directory
    ~/ironharness-firmware, where wsl-run.sh reads it (sed substitutes the path
    into the .resc)."""
    firmware = task.renode["firmware"]
    src = task.directory / firmware
    if not src.is_file():
        src = common.FIRMWARE_DIR / firmware
    if not src.is_file():
        raise ValueError(f"firmware {firmware!r} not found in the task directory or in tasks/_firmware/")
    common._push_to_wsl(common._tar_of(src, src.name), "$HOME/ironharness-firmware", "FW-PUSHED")


def _wsl_renode_cmd(remote_dir: str) -> list[str]:
    """Runs wsl-run.sh from the stage; stdin stays free (DEVNULL), logs go to stdout."""
    return [
        "wsl",
        "-d",
        common._wsl_distro(),
        "--",
        "bash",
        "-c",
        f"bash {remote_dir}/wsl-run.sh",
    ]


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


def _drive_repl(sock: socket.socket, task: Task, wall_deadline: float) -> tuple[str, str | None, str]:
    """Talks to the MicroPython REPL over the socket: paste the entry code, run
    the stimulus, collect the serial output.

    Returns (serial text, error|None, error_kind) - the classification is made
    at the source (IH-23): protocol failures (no REPL, no paste mode,
    unsupported step) are environment problems ("infra"), the anti-cheat
    verdict is a property of the agent's code ("run"). The protocol mirrors
    the wokwi paste scenario, but raw-paste is not supported by the litex
    firmware - legacy paste mode is used (Ctrl+E, the answer 'paste mode;
    Ctrl-C to cancel...').
    """
    parts: list[str] = []
    tel = _TelnetFilter()
    # nudge the REPL with an empty line: the prompt prints once and is easy to miss on connect
    sock.sendall(b"\n")
    buf, ok = _recv_until(sock, (common.REPL_PROMPT,), wall_deadline, tel)
    parts.append(buf)
    if not ok:
        return "".join(parts), "REPL is not responding (no '>>>' prompt)", common.ERROR_INFRA

    sock.sendall(b"\x05")
    buf, ok = _recv_until(
        sock, ("paste mode",), min(wall_deadline, time.monotonic() + RENODE_STEP_SEC), tel
    )
    parts.append(buf)
    if not ok:
        return (
            "".join(parts),
            "paste mode unavailable (firmware without Ctrl+E)",
            common.ERROR_INFRA,
        )

    code = (task.directory / task.entry).read_text(encoding="utf-8")
    _send_chunked(sock, _paste_code(code).encode("utf-8") + b"\n\x04")

    # stimulus steps; set-control (Wokwi buttons) cannot be reproduced under renode
    for step in task.stimulus:
        if time.monotonic() > wall_deadline:
            break
        if "set-control" in step:
            return (
                "".join(parts),
                f"set-control step not supported by the renode target: {step}",
                common.ERROR_INFRA,
            )
        if "delay" in step:
            time.sleep(min(common._parse_delay(step["delay"]), max(0.0, wall_deadline - time.monotonic())))
        elif "write-serial" in step:
            sock.sendall(str(step["write-serial"]).encode("utf-8"))
        elif "wait-serial" in step:
            needle = str(step["wait-serial"])
            # anti-cheat (IH-14): unlike unix/real (chunk ingestion stamps), here
            # the check is positional over everything read so far - INCLUDING the
            # echo of the pasted firmware source, so a needle that appears as a
            # literal inside the source (print("...")) would flag too. Renode
            # goldens have no stimulus yet; revisit before adding any.
            if needle in "".join(parts):
                return "".join(parts), (
                    f"anti-cheat: {needle!r} was printed before the stimulus asked "
                    "for it (pre-printed output)"
                ), common.ERROR_RUN
            buf, _ = _recv_until(sock, (needle,), wall_deadline, tel)
            parts.append(buf)

    # keep reading: until the full set of literal expects is collected (early
    # exit, like wait-serial in the wokwi scenario), until the prompt returns
    # (a finite program finished), or until the deadline (infinite firmware loop)
    plain = tuple(p for p in task.expect if common._plain_text(p))
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
        if (plain and all(p in buf for p in plain)) or common.REPL_PROMPT in buf:
            break
    parts.append(buf)
    return "".join(parts), None, common.ERROR_NONE


def _run_renode(
    task: Task,
    *,
    out_dir: Path,
    renode_cmd: str | list | None = None,
    journal=None,
) -> common.TaskResult:
    """Runs the task in Renode and returns the result (pass/fail + reason).

    renode_cmd=None -> the standard WSL path (firmware and stage go as tar
    blobs via communicate, then wsl-run.sh starts and reports the port);
    command injection (tests) runs the fake "Renode" locally, without WSL.
    """
    if "set-control" in {k for step in task.stimulus for k in step}:
        result = common.TaskResult(
            task=task.name,
            passed=False,
            exit_code=None,
            duration_sec=0.0,
            serial_log=None,
            missed=tuple(task.expect),
            error="set-control is not supported by the renode target (Wokwi buttons)",
            error_kind=common.ERROR_INFRA,
        )
        common._journal_result(journal, result)
        return result

    run_dir, _ = common._new_run_dir(out_dir, task)
    serial_log = run_dir / f"{task.name}.serial.log"
    port = int(os.environ.get("IRONBENCH_RENODE_PORT", RENODE_PORT))
    wall_timeout = task.timeout_sec * 2 + common.WALL_GRACE_SEC
    start = time.monotonic()
    exit_code: int | None = None
    error: str | None = None
    error_kind = common.ERROR_NONE
    serial_text = ""
    proc = None
    try:
        stage = _stage_renode_task(task, run_dir, port)
        if renode_cmd is None:
            _push_firmware(task)
            common._push_to_wsl(
                _tar_of_files(stage), f"{common.RENODE_REMOTE_ROOT}/{task.name}", "STAGE-PUSHED"
            )
            cmd = _wsl_renode_cmd(f"{common.RENODE_REMOTE_ROOT}/{task.name}")
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
            serial_text, error, error_kind = _drive_repl(
                sock, task, time.monotonic() + wall_timeout
            )
        exit_code = 0 if error is None else None
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
        error = f"I/O error while starting Renode: {e}"
        error_kind = common.ERROR_INFRA
    finally:
        if proc is not None:
            common._reap(proc)
            if proc.stdout is not None:
                proc.stdout.close()

    serial_log.write_text(serial_text, encoding="utf-8")
    duration = round(time.monotonic() - start, 2)
    missed, hit_fail = common._check_patterns(serial_text, task.expect, task.fail)
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


def _tar_of_files(stage: Path) -> bytes:
    """All stage files as one tar.gz blob (tiny: resc + wsl-run.sh)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for item in sorted(stage.iterdir()):
            tar.add(item, arcname=item.name)
    return buf.getvalue()


def _read_port_line(stream, timeout: float) -> int:
    """Renode socket-terminal port from the WSL pipeline stdout."""
    raw = common._read_marker_line(stream, "RENODE_PORT", timeout)
    if not raw.isdigit():  # junk after '=' - nothing more to read, fail honestly
        raise ConnectionError(f"WSL pipeline reported a non-numeric port: {raw!r}")
    return int(raw)
