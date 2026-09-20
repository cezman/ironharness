"""wokwi target (cloud): wokwi-cli drives the simulation, the runner stages
the task and scores the serial log.

MicroPython under wokwi-cli does not auto-run main.py (bare firmware + REPL),
so when a task has no static scenario the runner generates a REPL-paste
scenario: wait for the '>>>' prompt, paste the entry file code in raw-paste
mode (Ctrl+E/Ctrl+D) and wait for the expected lines. For golden tasks the
entry is solution.py (the reference); main.py is the file the benchmark agent
writes.

wokwi-cli exit codes: 0 - the scenario finished, 42 - the --timeout fired.
For firmware with an infinite loop 42 is normal, so both codes count as
success when all patterns match.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import yaml

from ironbench import runner_common as common
from ironbench.tasks import Task

# wokwi-cli exit codes: a fired --timeout (42) is normal for infinite firmware
WOKWI_TIMEOUT_EXIT = 42
OK_EXIT_CODES = (0, WOKWI_TIMEOUT_EXIT)


def default_cli() -> str:
    """wokwi-cli path: on PATH or the installer default location (~/.wokwi/bin)."""
    found = shutil.which("wokwi-cli")
    if found:
        return found
    exe = "wokwi-cli.exe" if sys.platform == "win32" else "wokwi-cli"
    candidate = Path.home() / ".wokwi" / "bin" / exe
    return str(candidate) if candidate.is_file() else "wokwi-cli"


def generate_paste_scenario(task: Task) -> str:
    """Scenario YAML: paste the entry file code into the REPL, run the stimulus, wait for expect."""
    code = (task.directory / task.entry).read_text(encoding="utf-8")
    steps: list[dict[str, object]] = [
        {"wait-serial": common.REPL_PROMPT},
        {"write-serial": "\x05"},  # Ctrl+E: raw-paste mode
        {"delay": "200ms"},
        {"write-serial": code},
        {"write-serial": "\x04"},  # Ctrl+D: execute
        *task.stimulus,  # interaction with the firmware (serial input, buttons, sensors)
    ]
    # wait-serial on literal expect patterns: the scenario finishes the
    # simulation early once everything expected has been printed (saves Wokwi quota)
    for pattern in task.expect:
        plain = common._plain_text(pattern)
        if plain:
            steps.append({"wait-serial": plain})
    doc = {
        "name": f"{task.name}-paste",
        "version": 1,
        "author": "ironbench",
        "steps": steps,
    }
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)


def _stage_task(task: Task, out_dir: Path) -> tuple[Path, str]:
    """Copies the task directory into a clean stage; returns (path, scenario name).
    IH-60: subdirectories are copied too (lib/ etc.) - a task referencing a
    helper module used to stage without it and crash on the paid simulation
    instead of failing at staging."""
    stage = out_dir / task.name
    shutil.rmtree(stage, ignore_errors=True)  # without this, stale files survive the run
    stage.mkdir(parents=True, exist_ok=True)
    for item in task.directory.iterdir():
        if item.is_file():
            shutil.copy2(item, stage / item.name)
        elif item.is_dir():
            shutil.copytree(item, stage / item.name)
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
            shared = common.FIRMWARE_DIR / name
            if shared.is_file():
                shutil.copy2(shared, stage / name)
            else:
                raise ValueError(
                    f"firmware {name!r} not found in the task directory or in tasks/_firmware/"
                )


def _truncate_paste_echo(text: str, task: Task) -> tuple[str, str | None, bool]:
    """IH-74 (review): the Ctrl+E paste scenario echoes the pasted source
    back into the serial log - the same class as IH-55 on renode. Expect
    literals inside the pasted code used to be credited from the echo alone
    (a firmware printing nothing passed). Cut everything through the last
    line of the pasted source; the runtime output starts after it. A lost
    echo tail (serial log cut mid-echo) cannot be scored - refuse as infra
    instead of crediting a partial echo. Trade-off (shared with the renode
    trim, fail-closed): a legitimate firmware whose source contains an
    expect literal verbatim may false-FAIL if the simulation ends before
    any runtime output - before the fix the same shape could false-PASS.

    The third return value (engaged) says the trim actually cut: a paste
    happened and the pasted source was found. The boot-dump verdict is only
    meaningful on a trimmed log - without the cut, a source line equal to a
    stimulus payload would fake the anchor (audit A review).
    """
    try:
        pasted = (task.directory / task.entry).read_text(encoding="utf-8")
    except OSError:
        return text, None, False
    last = [ln for ln in pasted.splitlines() if ln.strip()]
    if not last or "paste mode" not in text:
        # no paste happened (static scenario) or no echo at all - nothing to
        # trim and nothing suspect
        return text, None, False
    if last[0] not in text:
        return text, None, False
    pos = text.rfind(last[-1])
    if pos < 0:
        return (
            text,
            "paste echo truncated (serial log lost bytes) - the run cannot be scored",
            False,
        )
    return text[pos + len(last[-1]):], None, True


def _pre_printed_error(text: str, task: Task) -> str | None:
    """Position-based boot-dump verdict for the paste scenario (audit A, 2026-09-20).

    A wokwi serial log has no ingestion stamps (unix anchors on reader stamps,
    real on chunk stamps) - only the final text is scoreable. Callers pass the
    trimmed post-echo segment (the trim must have engaged). Anchor: the first
    whole line equal to a write-serial payload - MicroPython input() echoes the
    characters it reads line-fed, so an honest interaction shows the payload
    before the answer built on it (the line must not match inside an answer
    line: "echo: hello" contains "hello"). An expect literal whose first
    occurrence precedes the anchor was printed before the stimulus asked for
    it. The task declares its startup output via boot_expect (loader-validated
    expect members): those literals are exempt wherever they appear - glued
    banner dumps, bannerless dumps, decorated and suffixed banners are all
    condemned (fail-closed; the round-1/2 review history is in PR #92).

    Fail-open residuals (documented, marked non-equivalent on the leaderboard,
    never guessed here): a log without a payload echo (script-mode stdin
    without echo); regex-only expect patterns; multi-line payloads; CRLF is
    normalized once, so offsets stay exact.
    """
    text = text.replace("\r\n", "\n")
    lines = text.splitlines()
    for step in task.stimulus:
        if "write-serial" not in step:
            continue
        payload = str(step["write-serial"]).replace("\r\n", "\r").replace("\n", "\r").strip()
        if not payload:
            continue
        anchor = None
        offset = 0
        for line in lines:
            if line.strip() == payload:
                anchor = offset
                break
            offset += len(line) + 1
        if anchor is None:
            continue
        # Task-declared startup output (boot_expect): the firmware prints it
        # before any stimulus, so it is not an answer. Everything else found
        # before the anchor is condemned - glued banner dumps, bannerless
        # dumps, decorated banners (fail-closed by design; the task declares
        # its startup literals via boot_expect, validated as expect members).
        for pattern in task.expect:
            plain = common._plain_text(pattern)
            if not plain or plain in task.boot_expect:
                continue
            pos = text.find(plain)
            if 0 <= pos < anchor:
                return (
                    f"anti-cheat: {plain!r} was printed before the stimulus "
                    "asked for it (pre-printed output)"
                )
        return None
    return None


def _run_wokwi(
    task: Task,
    *,
    out_dir: Path,
    cli_path: str | None = None,
    token: str | None = None,
    journal=None,
) -> common.TaskResult:
    """Runs the task in Wokwi and returns the result (pass/fail + reason)."""
    cli = cli_path or default_cli()
    cli_cmd = [cli] if isinstance(cli, str) else list(cli)  # tests pass a command list
    run_dir, _ = common._new_run_dir(out_dir, task)
    serial_log = run_dir / f"{task.name}.serial.log"
    wall_timeout = task.timeout_sec * 2 + common.WALL_GRACE_SEC

    token = common.resolve_token(token)
    env = {**os.environ}
    if token:
        env["WOKWI_CLI_TOKEN"] = token

    start = time.monotonic()
    exit_code: int | None = None
    error: str | None = None
    error_kind = common.ERROR_NONE
    stage = scenario_name = None
    try:
        stage, scenario_name = _stage_task(task, run_dir)
    except (OSError, ValueError) as e:
        # missing entry/firmware, broken wokwi.toml - a clean FAIL instead of a crash
        error = f"failed to prepare task: {e}"
        error_kind = common.ERROR_INFRA
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
                error_kind = common.ERROR_INFRA
        except subprocess.TimeoutExpired:
            error = f"runner wall-clock timeout ({wall_timeout} s)"
            error_kind = common.ERROR_TIMEOUT
        except FileNotFoundError:
            error = f"wokwi-cli not found: {cli}"
            error_kind = common.ERROR_INFRA

    duration = round(time.monotonic() - start, 2)
    serial_text = (
        serial_log.read_text(encoding="utf-8", errors="replace") if serial_log.is_file() else ""
    )
    serial_text, echo_error, trim_engaged = _truncate_paste_echo(serial_text, task)
    if echo_error is not None:
        error, error_kind = echo_error, common.ERROR_INFRA
    if error is None and trim_engaged:
        dump_error = _pre_printed_error(serial_text, task)
        if dump_error is not None:
            error, error_kind = dump_error, common.ERROR_RUN
    missed, hit_fail = common._check_patterns(serial_text, task.expect, task.fail)
    passed = exit_code in OK_EXIT_CODES and not missed and not hit_fail and error is None
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
