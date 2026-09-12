"""Shared foundation of the ironbench task runner (IH-23 split): the result
type, pattern scoring, per-run artifact directories, env/config resolution and
the WSL plumbing used by more than one target module. The per-target backends
live in runner_wokwi / runner_renode / runner_unix / runner_plant /
runner_real; `ironbench.runner` is the facade tying them together.

Scoring belongs to the runner (not the Wokwi scenario): after every run the
serial log is re-checked against the expect/fail patterns from task.yaml.

Target modules read the names defined here through the module namespace
(`common.WALL_GRACE_SEC`) instead of importing them by value, so tests patch
one namespace (`ironbench.runner_common`) and every target honors it.
"""

from __future__ import annotations

import dataclasses
import io
import os
import re
import shutil
import subprocess
import tarfile
import threading
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ironbench.tasks import Task

# Wall-clock headroom on top of the sim timeout (cloud simulation
# startup). Module constant - tests patch it to avoid really waiting.
WALL_GRACE_SEC = 15

# MicroPython REPL prompt telling us the device is ready for pasting
REPL_PROMPT = ">>>"

# Shared directory of pinned firmware: do not duplicate a bin into the task
# directory; the runner stages it per the elf/firmware links in wokwi.toml
FIRMWARE_DIR = Path(__file__).resolve().parent / "tasks" / "_firmware"

# Harness-provided shim modules for the unix target (e.g. machine.py), staged
# next to the entry when a task declares `shim: <name>` (see tasks.SHIM_NAMES)
SHIMS_DIR = Path(__file__).resolve().parent / "shims"

# Where the task stage lands in WSL2 (drvfs automount is disabled in the distro)
RENODE_REMOTE_ROOT = "$HOME/ironharness-runs"

# error_kind values for TaskResult (structured classification, IH-15)
ERROR_NONE = "none"
ERROR_INFRA = "infra"
ERROR_TIMEOUT = "timeout"
ERROR_RUN = "run"


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
    # structured classification (IH-15): "none" (no error), "infra"
    # (environment-level, the agent cannot fix it), "timeout" (wall limit),
    # "run" (a run result of the agent's code - crash, cheat, bad exit).
    # Solve decisions (early exit) must read THIS field, never parse error text.
    error_kind: str = ERROR_NONE


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


def _plain_text(pattern: str) -> str | None:
    """A pattern without regex metacharacters also works as wait-serial (early finish)."""
    return pattern if not re.search(r"[\\^$.|?*+()\[\]{}]", pattern) else None


def first_occurrence_stamp(
    text: str,
    chunks: list[tuple[float, str]],
    needle: str,
    since: float | None,
) -> float | None:
    """Ingestion stamp of the chunk holding the first occurrence of `needle`,
    or None when it is not printed yet / was printed unprompted (stamped
    before `since`; since=None = no stimulus write happened yet).

    The caller guarantees text and chunks stay position-consistent (every
    chunk was appended to text in order). Only a strictly-earlier stamp
    condemns: an ingestion stamp EQUAL to the trigger stamp means write ->
    firmware read -> echo -> ingest completed within one monotonic clock tick
    (field-proven on coarse-clock Windows CI, where a legal fast answer was
    condemned as pre-printed). Same-tick order is unknowable, so the benefit
    of the doubt goes to the agent. Shared by the unix target (reader thread
    ingests continuously) and the real target (IH-33 chunk-stamp anchor; the
    real runner drains the board buffer before recording a trigger stamp so
    stale buffered output cannot masquerade as a fresh answer).
    """
    pos = text.find(needle)
    if pos < 0:
        return None
    seen = 0
    stamp_hit: float | None = None
    for stamp, chunk in chunks:
        seen += len(chunk)
        if pos < seen:
            stamp_hit = stamp
            break
    if stamp_hit is None or since is None:
        return None
    return stamp_hit if stamp_hit >= since else None


def _parse_delay(value: str) -> float:
    """'1500ms' -> 1.5, '2s' -> 2.0; no suffix means seconds."""
    text = str(value).strip().lower()
    if text.endswith("ms"):
        return int(text[:-2]) / 1000
    if text.endswith("s"):
        return float(text[:-1])
    return float(text)


def _check_patterns(
    serial_text: str, expect: tuple[str, ...], fail: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    missed = tuple(p for p in expect if not re.search(p, serial_text))
    hit_fail = tuple(p for p in fail if re.search(p, serial_text))
    return missed, hit_fail


# Per-run artifacts land under out_dir/<task>/run-<id>/; the out dir carries
# this marker so cleanup tooling only ever removes ironbench's own files.
OUT_MARKER = ".ironbench-out"


def _new_run_dir(out_dir: Path, task: Task) -> tuple[Path, str]:
    """A fresh per-run artifact directory (serial log, controller cwd, stage).

    A run writes only into its own run-<id> directory, so a re-run into the same
    out_dir scores its own output: fixed per-task paths used to leak a stale
    serial log from a previous run into a false PASS. (The old plant worker
    additionally stamped a result.json with run_id; since IH-25 the plant
    scores in the harness from pipe data and writes no result file at all.)
    An uuid collision is retried; any other OSError (broken volume) propagates
    loudly - without an artifact directory there is nothing to score, so a
    fabricated FAIL would be no more honest than a crash.
    """
    for _ in range(3):
        run_id = uuid.uuid4().hex[:12]
        run_dir = out_dir / task.name / f"run-{run_id}"
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:  # astronomically unlikely - just draw again
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / OUT_MARKER).write_text("ironbench run artifacts\n", encoding="utf-8")
        return run_dir, run_id
    raise OSError(f"cannot create a unique run directory under {out_dir}")


def clean_runs(out_dir: Path, *, keep: int = 1) -> int:
    """Removes old per-run artifact directories, keeping the newest `keep` per
    task ("newest" = the largest directory mtime, i.e. the most recently
    touched). Only run-* dirs under an ironbench-marked out root are removed -
    the marker certifies the root, so keep foreign data out of the out dir -
    and a root without the marker is refused entirely. Do not run clean while
    a run is in progress: a long-running run's dir may look stale by mtime and
    get removed under it (the run then fails, it does not corrupt anything)."""
    if keep < 0:
        raise ValueError(f"keep must be >= 0, got {keep}")
    if not (out_dir / OUT_MARKER).is_file():
        raise ValueError(
            f"{out_dir} has no {OUT_MARKER} marker - not an ironbench out dir, refusing to clean"
        )
    removed = 0
    for task_dir in sorted(p for p in out_dir.iterdir() if p.is_dir()):
        runs = sorted(task_dir.glob("run-*"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in runs[keep:]:
            shutil.rmtree(stale, ignore_errors=False)
            removed += 1
    return removed


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
                "error_kind": result.error_kind,
            },
        )


def _wsl_distro() -> str:
    return os.environ.get("IRONBENCH_RENODE_DISTRO", "OpenClawGateway")


def _tar_of(path: Path, arcname: str) -> bytes:
    """One file as a tar.gz blob (to pass into WSL stdin via communicate)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(path, arcname=arcname)
    return buf.getvalue()


def _tar_pairs(items: list[tuple[Path, str]]) -> bytes:
    """Several files as one tar.gz blob (unix target: entry + shim module)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, arcname in items:
            tar.add(path, arcname=arcname)
    return buf.getvalue()


def _push_to_wsl(blob: bytes, remote_dir: str, marker: str, *, clean: bool = False) -> None:
    """tar.gz blob into WSL stdin (via communicate: the wsl.exe relay is only
    reliable that way); the marker in stdout confirms the extraction.
    clean=True wipes the remote dir first - for per-run dirs whose stale
    files (e.g. a leftover machine.py from a removed `shim:`) must not leak
    into the next run. Never use it on shared directories."""
    rm_part = "rm -rf {remote_dir} && " if clean else ""
    try:
        proc = subprocess.run(
            [
                "wsl",
                "-d",
                _wsl_distro(),
                "--",
                "bash",
                "-c",
                f"{rm_part}mkdir -p {remote_dir} && tar -xzf - -C {remote_dir} && echo {marker}",
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
