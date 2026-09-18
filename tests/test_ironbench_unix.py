"""Tests of the unix target (the MicroPython unix port in WSL2): a fake micropython -
a local script reading stdin and printing to stdout. No WSL, no build.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

import ironbench.runner as runner_module
from io_core.journal import JsonlJournal
from ironbench import runner_common
from ironbench.runner import run_task
from ironbench.tasks import load_task

# Fake micropython: prints a banner, then echoes lines from stdin (like uart-echo).
# Modes: echo / missed (a banner and exit) / crash (a non-zero exit) / hang (an
# eternal loop without stdin) / finite (a banner and a clean exit 0).
FAKE_UPY = textwrap.dedent(
    """
    import sys, time
    mode = sys.argv[1]
    print("boot ok", flush=True)
    if mode == "crash":
        sys.exit(3)
    if mode == "finite":
        sys.exit(0)
    if mode == "hang":
        while True:
            time.sleep(0.2)
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        print("echo: " + line.strip(), flush=True)
    """
)


def make_unix_task(tmp_path, expect=("boot ok",), stimulus=(), timeout_sec=5):
    d = tmp_path / "t"
    d.mkdir()
    text = f"""
name: fake-unix
description: fake
entry: solution.py
target: unix
timeout_sec: {timeout_sec}
expect:
"""
    text += "".join(f"  - {p!r}\n" for p in expect)
    if stimulus:
        text += "stimulus:\n" + "\n".join(f"  - {s}" for s in stimulus) + "\n"
    (d / "task.yaml").write_text(textwrap.dedent(text), encoding="utf-8")
    (d / "solution.py").write_text('print("hi")\n', encoding="utf-8")
    return load_task(d)


def run_fake_unix(tmp_path, task, mode, **kw):
    script = tmp_path / "fake_upy.py"
    script.write_text(FAKE_UPY, encoding="utf-8")
    cmd = [sys.executable, str(script), mode]
    return run_task(task, out_dir=tmp_path / "out", unix_cmd=cmd, **kw)


@pytest.fixture(autouse=True)
def _fast_deadlines(monkeypatch):
    # the missed tests wait until the deadline by design - do not wait 25 real seconds
    monkeypatch.setattr(runner_common, "WALL_GRACE_SEC", 1)


def test_unix_pass_with_stimulus_cr_translation(tmp_path):
    # \r from the wokwi-style stimulus is translated to \n: the echo answers the line.
    # The firmware is infinite - after the match it gets killed, exit_code None
    # (stdin is not closed: EOF at input() would put a Traceback in the log)
    task = make_unix_task(
        tmp_path,
        expect=("boot ok", "echo: hello"),
        stimulus=['delay: 50ms', 'write-serial: "hello\\r"'],
    )
    res = run_fake_unix(tmp_path, task, "echo")
    assert res.passed, res.error
    assert res.exit_code is None
    assert res.missed == ()
    assert res.serial_log is not None and "echo: hello" in res.serial_log.read_text("utf-8")
    assert "Traceback" not in res.serial_log.read_text("utf-8")


def test_unix_finite_program_exits_zero(tmp_path):
    task = make_unix_task(tmp_path)
    res = run_fake_unix(tmp_path, task, "finite")
    assert res.passed
    assert res.exit_code == 0


def test_unix_fail_on_missed_pattern(tmp_path):
    task = make_unix_task(tmp_path, expect=("never printed",), timeout_sec=1)
    res = run_fake_unix(tmp_path, task, "echo")
    assert not res.passed
    assert res.missed == ("never printed",)


def test_unix_fail_on_missed_pattern_with_expected_present(tmp_path):
    task = make_unix_task(tmp_path, expect=("boot ok", "nope"), timeout_sec=1)
    res = run_fake_unix(tmp_path, task, "echo")
    assert not res.passed
    assert res.missed == ("nope",)


def test_unix_nonzero_exit_is_error(tmp_path):
    task = make_unix_task(tmp_path)
    res = run_fake_unix(tmp_path, task, "crash")
    assert not res.passed
    assert res.exit_code == 3
    assert "exited with code 3" in (res.error or "")


def test_unix_hang_until_wall_deadline(tmp_path):
    # the pattern is never printed - the task fails on the deadline, the process is killed
    task = make_unix_task(tmp_path, expect=("never printed",), timeout_sec=1)
    res = run_fake_unix(tmp_path, task, "hang")
    assert not res.passed
    assert res.exit_code is None  # killed on the deadline, did not exit on its own
    assert res.missed == task.expect


def test_never_reading_firmware_cannot_hang_the_runner_past_deadline(tmp_path, monkeypatch):
    """IH-39: a firmware that never reads stdin + a stimulus larger than the
    OS pipe buffer used to block the runner's main thread inside a synchronous
    stdin write - the wall deadline checks live on that same thread, so the
    run hung forever. The run must end (clean FAIL) within the wall deadline."""
    monkeypatch.setattr(runner_common, "WALL_GRACE_SEC", 1)
    big = "X" * 500_000 + "\r"  # far beyond any default pipe buffer
    task = make_unix_task(
        tmp_path, expect=("never printed",), timeout_sec=1, stimulus=[f'write-serial: "{big}"']
    )
    box: dict = {}
    wall = 1 * 2 + 1  # timeout_sec * 2 + WALL_GRACE_SEC

    def go():
        box["res"] = run_fake_unix(tmp_path, task, "hang")

    th = threading.Thread(target=go, daemon=True)
    th.start()
    th.join(timeout=wall + 15)
    assert not th.is_alive(), (
        "the runner hung past the wall deadline: the stimulus write blocked "
        "on a stdin the firmware never reads"
    )
    res = box["res"]
    assert not res.passed
    assert res.missed == task.expect
    assert res.duration_sec <= wall + 5


def test_noise_terminator_bypasses_the_fault_layer_offline(tmp_path):
    """IH-39 pin, offline (the golden noisy tests need WSL and are skipped on
    CI): the \r->\n terminator must reach the firmware even when the noise
    layer drops every payload write - a terminator that itself went through
    FaultyTransport would glue frames and the firmware would starve (that
    regression happened when the async stdin pump landed)."""
    task = make_unix_task(
        tmp_path,
        expect=("echo: hello",),
        timeout_sec=5,
        stimulus=['write-serial: "hello\\r"'],
    )
    # task.noise is loader-validated only on load; mutate programmatically
    task = dataclasses.replace(
        task, noise={"seed": 0, "faults": [{"action": "drop", "probability": 1.0}]}
    )
    res = run_fake_unix(tmp_path, task, "echo")
    log = (tmp_path / "out").rglob("*.serial.log")
    text = "".join(p.read_text(encoding="utf-8") for p in log)
    # the payload was dropped: "echo: hello" can never appear - but the
    # terminator was delivered unwrapped, so the firmware answered empty lines
    assert not res.passed
    assert res.missed == task.expect
    assert text.count("echo: ") >= 1, (
        "the terminator never reached the firmware - it went through the "
        "noise layer instead of bypassing it"
    )


def test_stdin_backlog_overflow_is_a_run_error(tmp_path, monkeypatch):
    """IH-39 review B1: a stimulus beyond the 1 MiB pump cap against a
    never-reading firmware latches the overflow - the run must end within the
    wall deadline and classify the undelivered stimulus as a run error, not
    an infra failure."""
    monkeypatch.setattr(runner_common, "WALL_GRACE_SEC", 1)
    big = "X" * (1 << 21)  # a single chunk larger than the whole 1 MiB cap
    task = make_unix_task(
        tmp_path, expect=("never printed",), timeout_sec=1, stimulus=[f'write-serial: "{big}"']
    )
    box: dict = {}
    wall = 1 * 2 + 1

    def go():
        box["res"] = run_fake_unix(tmp_path, task, "hang")

    th = threading.Thread(target=go, daemon=True)
    th.start()
    th.join(timeout=wall + 15)
    assert not th.is_alive(), "overflow did not stop the run from finishing"
    res = box["res"]
    assert not res.passed
    assert res.error_kind == "run"
    assert "stimulus not delivered" in (res.error or "")


def test_unbounded_firmware_output_is_capped_offline(tmp_path, monkeypatch):
    """IH-45: a firmware printing without end used to be accumulated whole in
    memory (bounded only by the wall deadline) and written whole to the
    serial log - while the plant target's flood protection (IH-25) had no
    unix counterpart. The retained serial text must be byte-capped."""
    monkeypatch.setattr(runner_common, "WALL_GRACE_SEC", 1)
    task = make_unix_task(tmp_path, expect=("never printed",), timeout_sec=1)
    script = tmp_path / "fake_upy.py"
    script.write_text(
        textwrap.dedent(
            """
            import sys
            while True:
                sys.stdout.write("X" * 4096 + "\\n")
                sys.stdout.flush()
            """
        ),
        encoding="utf-8",
    )
    res = run_task(
        task, out_dir=tmp_path / "out", unix_cmd=[sys.executable, str(script)]
    )
    assert not res.passed
    assert res.serial_log is not None
    assert res.serial_log.stat().st_size <= (1 << 20) + 65536, (
        f"serial log grew to {res.serial_log.stat().st_size} bytes - no output cap"
    )


def test_newline_less_flood_cannot_defeat_the_output_cap(tmp_path, monkeypatch):
    """IH-48 breaker P1: readline() with no size cap accumulated a single
    endless line unboundedly INSIDE readline - the 1 MiB retained-text cap
    never saw the bytes and the serial log on disk grew to the flood size."""
    monkeypatch.setattr(runner_common, "WALL_GRACE_SEC", 1)
    task = make_unix_task(tmp_path, expect=("never printed",), timeout_sec=1)
    script = tmp_path / "fake_upy.py"
    script.write_text(
        textwrap.dedent(
            """
            import sys
            sys.stdout.write("A" * (32 << 20))  # 32 MB, NO newline, then hang
            sys.stdout.flush()
            import time
            while True:
                time.sleep(0.2)
            """
        ),
        encoding="utf-8",
    )
    res = run_task(
        task, out_dir=tmp_path / "out", unix_cmd=[sys.executable, str(script)]
    )
    assert not res.passed
    assert res.serial_log is not None
    size = res.serial_log.stat().st_size
    assert size <= (1 << 20) + 65536, (
        f"a newline-less flood wrote {size} bytes to the serial log - "
        "readline had no length cap"
    )


def test_unix_set_control_rejected_at_load(tmp_path):
    # IH-38 review N1: a natively-authored unix task with set-control is an
    # authoring error - it surfaces at load_task, before any run.
    with pytest.raises(ValueError, match="set-control"):
        make_unix_task(tmp_path, stimulus=['set-control: "button0: true"'])


def test_unix_set_control_rejected_upfront(tmp_path):
    # a task mutated to set-control AFTER load still gets the clean infra
    # refusal from run_task (the solve loop early-exits on error_kind=infra)
    task = dataclasses.replace(
        make_unix_task(tmp_path), stimulus=({"set-control": "button0: true"},)
    )
    res = run_fake_unix(tmp_path, task, "echo")
    assert not res.passed
    assert "set-control" in (res.error or "")
    # the refusal is incurable for the agent - the solve loop must exit immediately, not burn iterations
    assert runner_module.is_infra_error(res)
    assert res.error_kind == "infra"


def test_run_error_with_infra_like_text_is_not_infra(tmp_path):
    # IH-15: firmware text that merely CONTAINS infra-like phrases ("not
    # found") is a run result of the agent's code - solve must keep iterating
    # instead of early-exiting on the environment.
    task = make_unix_task(tmp_path, expect=("boot ok",))
    fw = tmp_path / "fw.py"
    fw.write_text(
        "import sys\nprint('boot ok')\nprint('config not found')\nsys.exit(1)\n",
        encoding="utf-8",
    )
    res = run_task(task, out_dir=tmp_path / "out", unix_cmd=[sys.executable, str(fw)])
    assert not res.passed
    assert res.error_kind == "run"
    assert not runner_module.is_infra_error(res)


def test_missing_entry_is_kind_infra(tmp_path):
    task = make_unix_task(tmp_path, expect=("boot ok",))
    (task.directory / "solution.py").unlink()
    res = run_task(task, out_dir=tmp_path / "out")
    assert not res.passed
    assert res.error_kind == "infra"
    assert runner_module.is_infra_error(res)


def test_first_answer_stamp_same_tick_is_genuine():
    # CI field failure (windows runners): a legal fast answer can be ingested
    # within the same monotonic clock tick as the stimulus write. Same-tick
    # order is unknowable - the benefit of the doubt goes to the agent; only a
    # strictly-earlier stamp condemns as pre-printed.
    import threading

    box = {
        "text": "echo: hi\n",
        "chunks": [(100.0, "echo: hi\n")],
        "lock": threading.Lock(),
    }
    assert runner_module._first_answer_stamp(box, "echo: hi", 100.0) == 100.0
    assert runner_module._first_answer_stamp(box, "echo: hi", 101.0) is None
    assert runner_module._first_answer_stamp(box, "echo: hi", 99.0) == 100.0
    assert runner_module._first_answer_stamp(box, "echo: never", 100.0) is None


def test_unix_bin_env_override_and_default(monkeypatch):
    # IH-23 env pins: IRONBENCH_UNIX_BIN swaps the micropython binary in the
    # generated command; without it the pinned default is used. The env var is
    # read at call time (inside _unix_cmd), so both branches are testable.
    cmd_tail = lambda: runner_module._unix_cmd("$HOME/ironharness-runs/fake-unix/main.py")[-1]
    monkeypatch.setenv("IRONBENCH_UNIX_BIN", "~/opt/upy-custom")
    assert "~/opt/upy-custom" in cmd_tail()
    assert cmd_tail().startswith("exec ")
    monkeypatch.delenv("IRONBENCH_UNIX_BIN", raising=False)
    assert "~/bin/micropython" in cmd_tail()


def test_unexecutable_unix_cmd_is_kind_infra(tmp_path):
    # Popen fails outright (a directory is not executable): an environment
    # failure the agent cannot fix - classified infra, not "run"/"none".
    task = make_unix_task(tmp_path, expect=("boot ok",))
    res = run_task(task, out_dir=tmp_path / "out", unix_cmd=str(tmp_path))
    assert not res.passed
    assert res.error_kind == "infra"
    assert runner_module.is_infra_error(res)


def test_unix_journal_records_start_and_result(tmp_path):
    task = make_unix_task(tmp_path)
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr:
        run_fake_unix(tmp_path, task, "echo", journal=jr)
    events = [json.loads(line) for line in jpath.read_text("utf-8").splitlines()]
    kinds = [e["kind"] for e in events]
    assert "task_start" in kinds and "task_result" in kinds
    assert events[-1]["passed"] is True


def test_unix_without_cmd_pushes_entry_and_runs_wsl(tmp_path, monkeypatch):
    task = make_unix_task(tmp_path)
    runs, popens = [], []

    def fake_run(cmd, **kw):
        runs.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"STAGE-PUSHED\n", stderr=b"")

    def fake_popen(cmd, **kw):
        popens.append(cmd)
        raise FileNotFoundError("micropython")

    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    monkeypatch.setattr(runner_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(runner_common, "_wsl_home", lambda distro=None: "/home/tester")
    res = run_task(task, out_dir=tmp_path / "out")
    assert not res.passed
    assert "not found" in (res.error or "")
    assert len(runs) == 1 and "/home/tester/ironharness-runs" in runs[0][-1]
    assert popens[0][:3] == ["wsl", "-d", "OpenClawGateway"]
    assert "~/bin/micropython" in popens[0][-1]
    assert popens[0][-1].startswith("exec ")  # micropython replaces bash


def test_wsl_staging_resolves_home_before_quoting(tmp_path, monkeypatch):
    # IH-71: shlex.quote froze the literal $HOME on the staging side while the
    # run side (unquoted) expanded it - staging and run disagreed on the
    # directory. The root must be resolved to an absolute path BEFORE quoting.
    task = make_unix_task(tmp_path)
    bash_cmds, popens = [], []

    def fake_run(cmd, **kw):
        bash_cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"STAGE-PUSHED\n", stderr=b"")

    def fake_popen(cmd, **kw):
        popens.append(cmd)
        raise FileNotFoundError("micropython")

    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    monkeypatch.setattr(runner_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        runner_common, "_wsl_home", lambda distro=None: "/home/tester", raising=False
    )
    res = run_task(task, out_dir=tmp_path / "out")
    assert not res.passed
    # bash cannot expand $HOME inside single quotes: a literal $HOME in the
    # staging command builds a directory named `$HOME` in the WSL cwd
    assert "$HOME" not in bash_cmds[0][-1], bash_cmds[0][-1]
    assert "/home/tester/ironharness-runs" in bash_cmds[0][-1], bash_cmds[0][-1]
    assert "/home/tester/ironharness-runs" in popens[0][-1], popens[0][-1]


def test_wsl_home_probe_failure_is_infra_not_crash(tmp_path, monkeypatch):
    # IH-71 review: the $HOME probe failure raises ConnectionError, which the
    # runners classify as infra (TaskResult) - the CLI must not crash with an
    # uncaught exception when the distro is missing or broken.
    task = make_unix_task(tmp_path)

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"no such distro")

    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    res = run_task(task, out_dir=tmp_path / "out")
    assert not res.passed
    assert res.error_kind == "infra"
    assert res.error  # human-readable reason preserved


def test_mqtt_broker_cmd_quotes_remote_dir(tmp_path, monkeypatch):
    # IH-71 review: the broker command must quote the remote dir - a space or
    # a shell metacharacter in the task name must not word-split the command
    import dataclasses

    from ironbench import runner_common

    task = dataclasses.replace(make_unix_task(tmp_path), name="fake unix")
    bash_cmds, popen_cmds = [], []

    def fake_run(cmd, **kw):
        bash_cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"BROKER-PUSHED\n", stderr=b"")

    def fake_popen(cmd, **kw):
        popen_cmds.append(cmd)
        raise RuntimeError("stop here")

    monkeypatch.setattr(runner_common, "_wsl_home", lambda distro=None: "/home/tester")
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    monkeypatch.setattr(runner_module.subprocess, "Popen", fake_popen)
    with pytest.raises(RuntimeError):
        runner_module._start_wsl_mqtt_broker(task, 1883)
    assert (
        "python3 '/home/tester/ironharness-runs/fake unix-mqtt'/mqtt_sim.py"
        in popen_cmds[0][-1]
    ), popen_cmds[0][-1]


def test_unix_cli_target_override(tmp_path):
    # --target unix overrides the target of a wokwi task: the dispatcher goes to unix
    from ironbench.tasks import load_tasks

    tasks = load_tasks(Path(__file__).parents[1] / "src" / "ironbench" / "tasks")
    uart = next(t for t in tasks if t.name == "uart-echo")
    assert uart.target == "wokwi"
    import dataclasses

    assert dataclasses.replace(uart, target="unix").target == "unix"


def make_noise_task(tmp_path, noise_yaml, stimulus, expect=("echo: one",), timeout_sec=1):
    d = tmp_path / "tn"
    d.mkdir()
    text = f"""
name: fake-noise
description: fake
entry: solution.py
target: unix
timeout_sec: {timeout_sec}
{noise_yaml}
expect:
"""
    text += "".join(f"  - {p!r}\n" for p in expect)
    text += "stimulus:\n" + "\n".join(f"  - {s}" for s in stimulus) + "\n"
    (d / "task.yaml").write_text(textwrap.dedent(text), encoding="utf-8")
    (d / "solution.py").write_text('print("hi")\n', encoding="utf-8")
    return load_task(d)


def test_unix_noise_drop_swallows_write_step(tmp_path):
    # op2 is dropped by the line: the body of "two" never arrives, aaa/ccc arrive
    noise = """
noise:
  seed: 7
  faults:
    - action: drop
      after_ops: 1
      count: 1
"""
    stimulus = [
        'write-serial: "one\\r"',
        'write-serial: "two\\r"',
        'write-serial: "three\\r"',
    ]
    task = make_noise_task(
        tmp_path, noise, stimulus, expect=("echo: one", "echo: three")
    )
    res = run_fake_unix(tmp_path, task, "echo")
    assert res.passed, res.error
    log = res.serial_log.read_text("utf-8")
    assert "echo: two" not in log
    assert "echo: one" in log and "echo: three" in log


def test_unix_noise_corrupt_changes_bytes_then_retry_is_clean(tmp_path):
    # op1 gets every byte corrupted (ratio 1.0), the retry of op2 arrives clean
    noise = """
noise:
  seed: 7
  faults:
    - action: corrupt
      after_ops: 0
      count: 1
      ratio: 1.0
"""
    stimulus = [
        'write-serial: "one\\r"',
        'write-serial: "one\\r"',
    ]
    task = make_noise_task(tmp_path, noise, stimulus, expect=("echo: one",))
    res = run_fake_unix(tmp_path, task, "echo")
    assert res.passed, res.error
    log = res.serial_log.read_text("utf-8")
    assert "echo: one" in log
    assert log.count("echo: ") == 2  # the corrupted line was printed too, but different


def test_unix_noise_validation_bad_action(tmp_path):
    noise = """
noise:
  seed: 7
  faults:
    - action: explode
      count: 1
"""
    with pytest.raises(ValueError, match="noise.faults"):
        make_noise_task(tmp_path, noise, ['write-serial: "x\\r"'])


def test_unix_noise_validation_bad_seed(tmp_path):
    noise = """
noise:
  seed: abc
  faults: []
"""
    with pytest.raises(ValueError, match="noise.seed"):
        make_noise_task(tmp_path, noise, ['write-serial: "x\\r"'])


def test_unix_noise_validation_disconnect_rejected(tmp_path):
    # disconnect from the noisy line is not supported: the validator cuts it at load
    noise = """
noise:
  seed: 7
  faults:
    - action: disconnect
      after_ops: 1
"""
    with pytest.raises(ValueError, match="disconnect"):
        make_noise_task(tmp_path, noise, ['write-serial: "x\\r"'])


def test_unix_noise_rejected_on_wokwi_target(tmp_path):
    d = tmp_path / "tw"
    d.mkdir()
    text = """
name: fake-wokwi-noise
description: fake
entry: main.py
target: wokwi
timeout_sec: 5
noise:
  seed: 7
  faults:
    - action: drop
      count: 1
expect:
  - 'x'
"""
    (d / "task.yaml").write_text(textwrap.dedent(text), encoding="utf-8")
    with pytest.raises(ValueError, match="the unix target"):
        load_task(d)
