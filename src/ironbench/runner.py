"""ironbench task runner: dispatches a task to its target backend and scores
the serial log against the expect/fail patterns from task.yaml.

Targets (one module each, split out of this file in IH-23):

- runner_wokwi  - wokwi (cloud simulation via wokwi-cli)
- runner_renode - renode (MicroPython in Renode under WSL2, UART over TCP)
- runner_unix   - unix (MicroPython unix port in WSL2, free local runs)
- runner_plant  - plant (a pure-Python "plant + controller" loop)
- runner_real   - real (a live board, REPL over SerialTransport - see realhw.py)

runner_common holds what several targets share: the TaskResult type and
error_kind classification, pattern scoring, per-run artifact directories,
env/config resolution and the WSL plumbing.

This module is the stable import surface: `run_task` and every historical
runner name are re-exported here, so agents, the CLI and the tests keep
importing from `ironbench.runner`. Target backends are called through this
module's namespace, so tests can patch them here
(`monkeypatch.setattr(runner_module, "_run_wokwi", ...)`). Shared constants
that targets read through `runner_common` are patched there
(`monkeypatch.setattr(runner_common, "WALL_GRACE_SEC", ...)`); the renode
constants re-exported below are read by their own target through the
`runner_renode` namespace and are patched there (`runner_renode.RENODE_STEP_SEC`, ...).
"""

from __future__ import annotations

import subprocess  # noqa: F401  (tests patch/inspect it through this module)
from pathlib import Path

from ironbench.runner_common import (
    ERROR_INFRA,
    ERROR_NONE,
    ERROR_RUN,
    ERROR_TIMEOUT,
    FIRMWARE_DIR,
    OUT_MARKER,
    RENODE_REMOTE_ROOT,
    REPL_PROMPT,
    SHIMS_DIR,
    WALL_GRACE_SEC,
    TaskResult,
    _check_patterns,
    _journal_result,
    _new_run_dir,
    _parse_delay,
    _plain_text,
    _push_to_wsl,
    _read_marker_line,
    _reap,
    _tar_of,
    _tar_pairs,
    _wsl_distro,
    clean_runs,
    find_env_file,
    load_env_file,
    resolve_token,
)
from ironbench.runner_plant import _run_plant
from ironbench.runner_real import _run_real
from ironbench.runner_renode import (
    RENODE_CONNECT_SEC,
    RENODE_PORT,
    RENODE_STEP_SEC,
    _drive_repl,
    _paste_code,
    _push_firmware,
    _read_port_line,
    _recv_until,
    _run_renode,
    _send_chunked,
    _stage_renode_task,
    _tar_of_files,
    _TelnetFilter,
    _wsl_renode_cmd,
    _wsl_run_script,
    generate_renode_resc,
)
from ironbench.runner_unix import (
    MQTT_SIM_PATH,
    _check_events,
    _first_answer_stamp,
    _free_tcp_port,
    _run_unix,
    _start_wsl_mqtt_broker,
    _StdinWriter,
    _stop_broker_proc,
    _unix_cmd,
)
from ironbench.runner_wokwi import (
    OK_EXIT_CODES,
    WOKWI_TIMEOUT_EXIT,
    _run_wokwi,
    _stage_firmware,
    _stage_task,
    default_cli,
    generate_paste_scenario,
)
from ironbench.tasks import Task

__all__ = [
    "ERROR_INFRA",
    "ERROR_NONE",
    "ERROR_RUN",
    "ERROR_TIMEOUT",
    "FIRMWARE_DIR",
    "MQTT_SIM_PATH",
    "OK_EXIT_CODES",
    "OUT_MARKER",
    "RENODE_CONNECT_SEC",
    "RENODE_PORT",
    "RENODE_REMOTE_ROOT",
    "RENODE_STEP_SEC",
    "REPL_PROMPT",
    "SHIMS_DIR",
    "WALL_GRACE_SEC",
    "WOKWI_TIMEOUT_EXIT",
    "TaskResult",
    "_StdinWriter",
    "_TelnetFilter",
    "_check_events",
    "_check_patterns",
    "_drive_repl",
    "_first_answer_stamp",
    "_free_tcp_port",
    "_journal_result",
    "_new_run_dir",
    "_parse_delay",
    "_paste_code",
    "_plain_text",
    "_push_firmware",
    "_push_to_wsl",
    "_read_marker_line",
    "_read_port_line",
    "_reap",
    "_recv_until",
    "_run_plant",
    "_run_real",
    "_run_renode",
    "_run_unix",
    "_run_wokwi",
    "_send_chunked",
    "_stage_firmware",
    "_stage_renode_task",
    "_stage_task",
    "_start_wsl_mqtt_broker",
    "_stop_broker_proc",
    "_tar_of",
    "_tar_of_files",
    "_tar_pairs",
    "_unix_cmd",
    "_wsl_distro",
    "_wsl_renode_cmd",
    "_wsl_run_script",
    "clean_runs",
    "default_cli",
    "find_env_file",
    "generate_paste_scenario",
    "generate_renode_resc",
    "is_infra_error",
    "load_env_file",
    "resolve_token",
    "run_task",
]


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
        error_kind=ERROR_INFRA,
    )
    _journal_result(journal, result)
    return result


def is_infra_error(result_or_error: TaskResult | str | None) -> bool:
    """True when a run failed at the environment level (the agent cannot fix
    it) - used for an early exit from the solve loop, so LLM iterations are
    not burned on a hopeless environment.

    TaskResult inputs are classified by the structured error_kind the runner
    set while running the task. The raw-string path is legacy (kept for
    caller-supplied messages) and matches runner-generated phrases only -
    never rely on it for firmware/CLI text.
    """
    if isinstance(result_or_error, TaskResult):
        return result_or_error.error_kind == ERROR_INFRA
    error = result_or_error
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
        "IRONBENCH_REAL_PORT is not set",
    )
    return any(m in error for m in marks)
