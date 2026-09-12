"""Anti-cheat tests (IH-14): a fake "solution" that dumps the expected strings
must FAIL on every text task, and an open-loop constant controller must FAIL
on every plant task (the tasks carry mid-run disturbances for exactly this).

The verbatim cheater models an adversary with full knowledge of task.yaml: it
prints the literal expect patterns up front. It is defeated by the runner's
wait-serial anchor (an answer stamped before the stimulus write is a failed
run) and by regex expectations that do not match their own source. A legal
fast responder must keep passing - the anchor must not punish quick answers.
"""

from __future__ import annotations

import dataclasses
import sys
import textwrap
from pathlib import Path

import pytest

from ironbench.runner import run_task
from ironbench.tasks import load_task, load_tasks

TASKS_DIR = Path(__file__).parents[1] / "src" / "ironbench" / "tasks"

ALL_TASKS = sorted(load_tasks(TASKS_DIR), key=lambda t: t.name)
UNIX_TASKS = [t for t in ALL_TASKS if t.target == "unix"]
PLANT_TASKS = [t for t in ALL_TASKS if t.target == "plant"]


def run_with_entry(task, code: str, tag: str, tmp_path: Path):
    work = tmp_path / tag / "work"
    work.mkdir(parents=True)
    (work / "solution.py").write_text(code, encoding="utf-8")
    cheated = dataclasses.replace(task, directory=work, entry="solution.py")
    return run_task(cheated, out_dir=tmp_path / tag / "out")


def verbatim_source(task) -> str:
    """A cheater that prints the literal expect patterns (full yaml knowledge)."""
    if not task.expect:
        return "print('nothing to fake')\n"
    joined = ", ".join(repr(p) for p in task.expect)
    return f"for _s in ({joined}):\n    print(_s)\n"


@pytest.mark.parametrize("task", UNIX_TASKS, ids=lambda t: t.name)
def test_unix_verbatim_cheater_fails(task, tmp_path, wsl_unix_ready):
    # Vacuous-guard (IH-24): without a WSL distro these runs die with an
    # infra error and the test would pass VACUOUSLY - "the cheater failed"
    # when in fact nothing ran. Skip loudly instead of scoring a green lie.
    if not wsl_unix_ready:
        pytest.skip("needs a WSL distro with micropython")
    res = run_with_entry(task, verbatim_source(task), "cheat", tmp_path)
    assert not res.passed, (
        f"{task.name} is verbatim-cheatable (printed the expect strings and passed): "
        f"error={res.error!r}"
    )
    # anti-cheat pin (IH-24): a task whose stimulus carries a trigger (the
    # wait-serial anchor applies) must catch the dump AS CHEATING, not fail
    # it on some missed pattern. Tasks without a trigger keep plain scoring.
    has_trigger = any(
        "write-serial" in s or "mqtt-publish" in s for s in task.stimulus
    )
    if has_trigger:
        assert (res.error or "").startswith("anti-cheat:"), (
            f"{task.name}: dump cheater was not caught by the wait-serial anchor "
            f"(error={res.error!r})"
        )


@pytest.mark.parametrize("task", PLANT_TASKS, ids=lambda t: t.name)
def test_plant_open_loop_constant_fails(task, tmp_path):
    # A constant power tuned to the (hidden) plant parameters holds the setpoint
    # before the mid-run disturbance and loses it after - the tasks carry
    # disturbances precisely to fail this open-loop cheat.
    u = (task.plant["setpoint"] - task.plant["ambient"]) / task.plant["K"]
    res = run_with_entry(task, f"def control(t, y, setpoint):\n    return {u!r}\n", "cheat", tmp_path)
    assert not res.passed, (
        f"{task.name} is open-loop-cheatable (constant u={u:.3f} passed): "
        f"error={res.error!r}"
    )


# --- mechanism-level tests on a synthetic interactive task (local python) ---


SYNTH_STIMULUS = (
    "  - delay: 100ms\n"
    "  - write-serial: \"hi\\r\"\n"
    "  - wait-serial: \"echo: hi\"\n"
    "  - write-serial: \"again\\r\"\n"
    "  - wait-serial: \"echo: again\"\n"
)


def make_synthetic_echo_task(tmp_path, entry_code: str):
    """A minimal interactive unix task: echo lines, driven by wait-serial steps."""
    d = tmp_path / "t"
    d.mkdir()
    text = textwrap.dedent(
        """
    name: synth-echo
    description: fake
    entry: solution.py
    target: unix
    timeout_sec: 5
    expect:
      - 'boot ok'
      - 'echo: hi'
      - 'echo: again'
    stimulus:
    """
    )
    (d / "task.yaml").write_text(text + SYNTH_STIMULUS, encoding="utf-8")
    (d / "solution.py").write_text(entry_code, encoding="utf-8")
    return load_task(d)


RESPONDER = (
    "import sys\n"
    "print('boot ok', flush=True)\n"
    "while True:\n"
    "    line = sys.stdin.readline()\n"
    "    if not line:\n"
    "        break\n"
    "    print('echo: ' + line.strip(), flush=True)\n"
)
DUMP_EXIT = (
    "print('boot ok')\nprint('echo: hi')\nprint('echo: again')\n"
)
DUMP_ALIVE = (
    "import time\n"
    "print('boot ok', flush=True)\n"
    "print('echo: hi', flush=True)\n"
    "print('echo: again', flush=True)\n"
    "time.sleep(30)\n"
)


def run_synthetic(tmp_path, entry_code: str):
    task = make_synthetic_echo_task(tmp_path, entry_code)
    cmd = [sys.executable, str(task.directory / "solution.py")]
    return run_task(task, out_dir=tmp_path / "out", unix_cmd=cmd)


def test_legal_fast_responder_passes(tmp_path):
    # The blocker pin: a solution that answers the stimulus QUICKLY must pass -
    # the anti-cheat anchor classifies answers by emission time relative to the
    # stimulus write, not by how soon they arrive.
    res = run_synthetic(tmp_path, RESPONDER)
    assert res.passed, (res.error, res.missed)


FINITE_RESPONDER = (
    "import sys\n"
    "print('boot ok', flush=True)\n"
    "line = sys.stdin.readline()\n"
    "print('echo: ' + line.strip(), flush=True)\n"
    "line = sys.stdin.readline()\n"
    "print('echo: ' + line.strip(), flush=True)\n"
)


def test_finite_responder_that_answers_then_exits_passes(tmp_path):
    # Answers both waits and exits at once: the firmware's EOF can be observed
    # before the wait-serial evaluation runs. The verdict must come from the
    # ingestion stamps (genuine answers), never from the eof flag seen first -
    # an eof-first wait loop would convict this honest program as a zero-waits
    # dump (the check-at-least-once pin for the IH-23 canonical verdict).
    res = run_synthetic(tmp_path, FINITE_RESPONDER)
    assert res.passed, (res.error, res.missed)


def test_dump_and_exit_cheater_fails_with_preprinted(tmp_path):
    res = run_synthetic(tmp_path, DUMP_EXIT)
    assert not res.passed
    # Deterministic since IH-23: the verdict is made once, over the final log,
    # after the reader thread joined - the needles are in it and their first
    # occurrence predates the stimulus write, so "pre-printed" always wins
    # over the zero-waits fallback (the two wordings used to race, IH-28).
    assert res.error_kind == "run"
    assert (res.error or "").startswith("anti-cheat:")
    assert "pre-printed output" in (res.error or "")


def test_silent_exit_before_first_wait_fails_with_zero_waits(tmp_path):
    # The other deterministic verdict: the firmware answers nothing and exits -
    # the waited needle is absent from the final log, so the zero-waits rule
    # (dump without even dumping) is what fires.
    res = run_synthetic(tmp_path, "print('boot ok')\n")
    assert not res.passed
    assert res.error_kind == "run"
    assert (res.error or "").startswith("anti-cheat:")
    assert "exited before the first wait-serial" in (res.error or "")


def test_dump_of_second_wait_needle_fails_with_preprinted(tmp_path):
    # Direct pin for the dump branch of canonical rule (2): the cheater prints
    # only the SECOND wait's needle, so rule (1) stays silent (its waited
    # needle 'echo: hi' never appears in the log) and the "pre-printed"
    # wording must come from rule (2) - a wait-serial needle sitting in the
    # final log without ever being credited as a stimulus answer.
    res = run_synthetic(tmp_path, "print('echo: again')\n")
    assert not res.passed
    assert res.error_kind == "run"
    assert (res.error or "").startswith("anti-cheat:")
    assert "pre-printed output" in (res.error or "")


def test_mqtt_dump_before_any_trigger_fails_with_preprinted(tmp_path):
    # mqtt-publish anchors the anti-cheat like write-serial: a cheater that
    # dumps the awaited needle and exits without ever crediting the wait step
    # (the delay lets it die before the stimulus loop even starts driving) is
    # named pre-printed, not zero-waits - the needle sits in the final log,
    # never credited as a stimulus answer.
    from io_core.mqtt_sim import MqttSimBroker

    d = tmp_path / "t"
    d.mkdir()
    text = textwrap.dedent(
        """
    name: mqtt-dump
    description: fake
    entry: solution.py
    target: unix
    timeout_sec: 10
    mqtt:
      client_id: ironbench-dump
    expect:
      - 'READY'
    stimulus:
      - delay: 300ms
      - mqtt-publish: {topic: dev/cmd, payload: "go", retain: true}
      - wait-serial: "READY"
    """
    )
    (d / "task.yaml").write_text(text, encoding="utf-8")
    (d / "solution.py").write_text("print('READY')\n", encoding="utf-8")
    task = load_task(d)
    broker = MqttSimBroker()
    broker.start()
    try:
        res = run_task(
            task,
            out_dir=tmp_path / "out",
            unix_cmd=[sys.executable, str(d / "solution.py")],
            mqtt_broker=broker,
        )
    finally:
        broker.stop()
    assert not res.passed
    assert res.error_kind == "run"
    assert (res.error or "").startswith("anti-cheat:")
    assert "pre-printed output" in (res.error or "")


def test_dump_and_stay_alive_cheater_fails_with_anchor(tmp_path):
    res = run_synthetic(tmp_path, DUMP_ALIVE)
    assert not res.passed
    assert "pre-printed output" in (res.error or "")


# --- golden unix solutions must survive the anti-cheat (offline via WSL) ---


GOLDEN_UNIX = [
    "coop-scheduler",
    "debug-hysteresis",
    "debug-pinlock",
    "debug-ringbuf",
    "frame-corrupt",
    "noisy-frames",
    "uart-menu",
    "watchdog",
]


@pytest.mark.parametrize("name", GOLDEN_UNIX)
def test_golden_unix_survives_anticheat(name, tmp_path, wsl_unix_ready):
    # Regression guard for the anti-cheat: the real golden solutions answer the
    # stimulus and must keep passing with the anchor active.
    if not wsl_unix_ready:
        pytest.skip("needs a WSL distro with micropython")
    task = load_task(TASKS_DIR / name)
    res = run_task(task, out_dir=tmp_path / name)
    assert res.passed, (res.error, res.missed)


# --- collect-timeout gap: every log append must be a registered chunk ---


def test_mqtt_collect_timeout_gap_does_not_false_flag(tmp_path):
    # Review round-2 blocker: the collect-timeout line ('received 0 of 1') was
    # appended to the log without a chunk registration - positions shifted and
    # honest output printed after the gap was misattributed as pre-printed
    # cheating. The firmware here answers only after the collect timeout, so
    # the answer's chunk lies beyond the unregistered-append position.
    from io_core.mqtt_sim import MqttSimBroker

    d = tmp_path / "t"
    d.mkdir()
    text = textwrap.dedent(
        """
    name: mqtt-gap
    description: fake
    entry: solution.py
    target: unix
    timeout_sec: 15
    mqtt:
      client_id: ironbench-gap
    expect:
      - 'boot'
      - 'READY'
    stimulus:
      - delay: 100ms
      - write-serial: "go\\r"
      - mqtt-collect: {topic: never/pub, count: 1, timeout_sec: 1}
      - wait-serial: "READY"
    """
    )
    (d / "task.yaml").write_text(text, encoding="utf-8")
    (d / "solution.py").write_text(
        "import sys, time\n"
        "print('boot', flush=True)\n"
        "sys.stdin.readline()\n"
        "print('go-ack', flush=True)\n"
        "time.sleep(1.2)\n"  # READY lands after the collect timeout line
        "print('READY', flush=True)\n",
        encoding="utf-8",
    )
    task = load_task(d)
    broker = MqttSimBroker()
    broker.start()
    try:
        res = run_task(
            task,
            out_dir=tmp_path / "out",
            unix_cmd=[sys.executable, str(d / "solution.py")],
            mqtt_broker=broker,
        )
    finally:
        broker.stop()
    assert res.passed, (res.error, res.missed)
