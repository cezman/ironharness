"""Tests of loading ironbench tasks from YAML."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from ironbench.tasks import load_task, load_tasks

VALID = """
name: blink
description: a test task
timeout_sec: 15
scenario: scenario.yaml
expect:
  - 'blink 0: on'
fail:
  - 'Traceback'
"""


def write_task(tmp_path, content=VALID, dirname="blink"):
    d = tmp_path / dirname
    d.mkdir()
    (d / "task.yaml").write_text(content, encoding="utf-8")
    return d


def test_load_task_parses_fields(tmp_path):
    d = write_task(tmp_path)
    task = load_task(d)
    assert task.name == "blink"
    assert task.timeout_sec == 15
    assert task.expect == ("blink 0: on",)
    assert task.fail == ("Traceback",)
    assert task.directory == d


def test_load_task_defaults(tmp_path):
    d = write_task(tmp_path, "name: t1\nexpect:\n  - 'x'\n")
    task = load_task(d)
    assert task.timeout_sec == 30
    assert task.scenario is None  # no scenario -> the runner generates REPL-paste
    assert task.entry == "main.py"
    assert task.expect == ("x",)
    assert task.stimulus == ()


def test_load_task_stimulus(tmp_path):
    content = "name: t1\nexpect:\n  - 'x'\nstimulus:\n  - delay: 500ms\n  - write-serial: \"hi\\n\"\n"
    task = load_task(write_task(tmp_path, content))
    assert task.stimulus == ({"delay": "500ms"}, {"write-serial": "hi\n"})


def test_load_task_stimulus_must_be_steps(tmp_path):
    d = write_task(tmp_path, "name: t1\nexpect:\n  - 'x'\nstimulus:\n  - just a string\n")
    with pytest.raises(ValueError, match="stimulus"):
        load_task(d)


def test_load_task_stimulus_unknown_step_key(tmp_path):
    d = write_task(tmp_path, "name: t1\nexpect:\n  - 'x'\nstimulus:\n  - write-seral: 'x'\n")
    with pytest.raises(ValueError, match="write-seral"):
        load_task(d)


def test_load_task_requires_name(tmp_path):
    d = write_task(tmp_path, "timeout_sec: 5\nexpect:\n  - 'x'\n")
    with pytest.raises(ValueError, match="name"):
        load_task(d)


def test_load_task_expect_must_be_strings(tmp_path):
    d = write_task(tmp_path, "name: t1\nexpect:\n  - 42\n")
    with pytest.raises(ValueError, match="expect"):
        load_task(d)


def test_load_tasks_sorted_and_skips_dirs_without_task(tmp_path):
    write_task(tmp_path, "name: bbb\nexpect:\n  - 'x'\n", dirname="bbb")
    write_task(tmp_path, "name: aaa\nexpect:\n  - 'x'\n", dirname="aaa")
    (tmp_path / "empty_dir").mkdir()
    tasks = load_tasks(tmp_path)
    assert [t.name for t in tasks] == ["aaa", "bbb"]


def test_load_tasks_missing_dir(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        load_tasks(tmp_path / "nope")


def test_load_task_tags_and_level(tmp_path):
    write_task(tmp_path, "name: aaa\nexpect:\n  - 'x'\ntags: [io, fsm]\nlevel: 3\n", dirname="aaa")
    task = load_task(tmp_path / "aaa")
    assert task.tags == ("io", "fsm")
    assert task.level == 3


def test_load_task_rejects_unknown_tag(tmp_path):
    write_task(tmp_path, "name: aaa\nexpect:\n  - 'x'\ntags: [robots]\n", dirname="aaa")
    with pytest.raises(ValueError, match="unknown tags"):
        load_task(tmp_path / "aaa")


def test_load_task_rejects_duplicate_tags(tmp_path):
    write_task(tmp_path, "name: aaa\nexpect:\n  - 'x'\ntags: [io, io]\n", dirname="aaa")
    with pytest.raises(ValueError, match="duplicates"):
        load_task(tmp_path / "aaa")


def test_load_task_rejects_bad_level(tmp_path):
    write_task(tmp_path, "name: aaa\nexpect:\n  - 'x'\nlevel: 9\n", dirname="aaa")
    with pytest.raises(ValueError, match="level"):
        load_task(tmp_path / "aaa")


def test_load_task_notes_roundtrip(tmp_path):
    # IH-21 core: expert notes load into the Task and ride into the prompt
    write_task(
        tmp_path,
        "name: aaa\nexpect:\n  - 'x'\nnotes:\n  - 'first lesson'\n  - 'second lesson'\n",
        dirname="aaa",
    )
    assert load_task(tmp_path / "aaa").notes == ("first lesson", "second lesson")


def test_load_task_notes_optional_and_empty_section_ok(tmp_path):
    write_task(tmp_path, "name: aaa\nexpect:\n  - 'x'\nnotes:\n", dirname="aaa")
    assert load_task(tmp_path / "aaa").notes == ()


@pytest.mark.parametrize("raw", ["notes: 'one string'\n", "notes:\n  - ''\n", "notes:\n  - 7\n"])
def test_load_task_rejects_bad_notes(tmp_path, raw):
    write_task(tmp_path, f"name: aaa\nexpect:\n  - 'x'\n{raw}", dirname="aaa")
    with pytest.raises(ValueError, match="notes"):
        load_task(tmp_path / "aaa")


def test_golden_tasks_have_tags_and_levels():
    from pathlib import Path

    tasks = load_tasks(Path(__file__).parents[1] / "src" / "ironbench" / "tasks")
    assert all(t.tags for t in tasks), "every golden task must have a class"
    assert all(t.level is not None for t in tasks), "every golden task must have a level"


# --- repository golden tasks: description integrity ---


def test_golden_tasks_all_load_and_include_expectations():
    from pathlib import Path

    tasks = load_tasks(Path(__file__).parents[1] / "src" / "ironbench" / "tasks")
    names = {t.name for t in tasks}
    assert {
        "blink",
        "uart-echo",
        "debounce",
        "sensor-poll",
        "protocol",
        "protocol-retry",
        "traffic-light",
        "noisy-frames",
        "frame-corrupt",
        "uart-menu",
        "watchdog",
        "coop-scheduler",
        "debug-ringbuf",
        "debug-hysteresis",
        "debug-pinlock",
        "mqtt-device",
        "adc-read",
        "median-filter",
        "threshold-hysteresis",
        "p-regulator",
        "pid-antiwindup",
        "system-id",
        "bme-read",
        "cross-sensor",
        "bus-diagnose",
    } <= names


def test_bme_read_is_real_target_with_notes():
    # IH-33 pin: the first live-board golden task stays target "real" and
    # carries bench notes - the solve agent's only edge on real hardware
    from pathlib import Path

    task = load_task(
        Path(__file__).parents[1] / "src" / "ironbench" / "tasks" / "bme-read"
    )
    assert task.target == "real"
    assert task.tags == ("data",)
    assert task.notes, "the bench lessons must ship with the task (IH-21)"
    # the wait is anchored to the request line: unique, task-specific needle
    # (a short 'T=' would false-match substrings of arbitrary agent output),
    # real trigger before it
    waits = [s["wait-serial"] for s in task.stimulus if "wait-serial" in s]
    assert waits == ["hPa"]
    assert any("write-serial" in s for s in task.stimulus)
    # sub-zero benches are legal: the temperature pattern accepts a sign
    assert any("-?" in p for p in task.expect)


def test_bus_diagnose_is_real_target_with_notes():
    # IH-91 pin: the degradation-diagnosis golden stays target "real", keeps
    # the bench notes, and its fail patterns include the exact failure mode
    # of the station firmware it diagnoses (unhandled ENODEV at boot)
    from pathlib import Path

    task = load_task(
        Path(__file__).parents[1] / "src" / "ironbench" / "tasks" / "bus-diagnose"
    )
    assert task.target == "real"
    assert task.tags == ("resilience",)
    assert task.level == 3
    assert task.notes, "the bench lessons must ship with the task (IH-21)"
    waits = [s["wait-serial"] for s in task.stimulus if "wait-serial" in s]
    assert waits == ["MISSING="]
    assert any("write-serial" in s for s in task.stimulus)
    # the scan report is pinned with an escaped bracket class, not a bare
    # literal (a bare 'SCAN=[60, 118]' regex is a char class matching 'SCAN=6')
    assert r"SCAN=\[60, 118\]" in task.expect
    # the healthy report lines are pinned in full: a partial revert of the
    # bench-state coupling (one line left from the degraded variant) must
    # fail here, not only on the live bench
    assert "MISSING=none" in task.expect
    assert "STATUS=ok" in task.expect
    assert "ENODEV" in task.fail
    assert "Traceback" in task.fail


def test_bus_diagnose_answer_is_not_leaked_by_the_prompt():
    # the task is a diagnosis only if the answer is not in the prompt: the
    # description names the EXPECTED device set (0x76, 0x3C) and the output
    # contract, while the observed bus state (which devices answer, the
    # exact report lines) lives only in expect/ - a firmware that hardcodes
    # the contract answer from the prompt must fail the degraded-vs-healthy
    # distinction (pinned end-to-end by test_bus_diagnose_degraded_guess_fails)
    from pathlib import Path

    raw = (
        Path(__file__).parents[1] / "src" / "ironbench" / "tasks" / "bus-diagnose" / "task.yaml"
    ).read_text(encoding="utf-8")
    prompt_sections = raw.split("expect:", 1)[0]
    assert "SCAN=[60, 118]" not in prompt_sections
    assert "MISSING=none" not in prompt_sections
    assert "STATUS=ok" not in prompt_sections


def test_wokwi_stimulus_controls_exist_in_diagram():
    # set-control references only parts from the task's diagram.json (catches typos in ids)
    import json
    from pathlib import Path

    tasks = load_tasks(Path(__file__).parents[1] / "src" / "ironbench" / "tasks")
    checked = 0
    for task in tasks:
        if task.target != "wokwi":
            continue
        diagram = json.loads((task.directory / "diagram.json").read_text("utf-8"))
        part_ids = {p["id"] for p in diagram["parts"]}
        for step in task.stimulus:
            if "set-control" in step:
                assert step["set-control"]["part-id"] in part_ids, task.name
                checked += 1
    assert checked >= 10  # the button/sensor tasks are really covered


def test_frame_corrupt_stimulus_checksums_are_honest():
    # retries in the stimulus must carry a correct xor2, otherwise the reference will not solve the task
    import re
    from pathlib import Path

    task = load_task(Path(__file__).parents[1] / "src" / "ironbench" / "tasks" / "frame-corrupt")
    checked = 0
    for step in task.stimulus:
        raw = str(step.get("write-serial", ""))
        m = re.fullmatch(r"#(\w+):(\w+):([0-9a-f]{2})\r?", raw)
        if not m:
            continue  # a frame fragment without xor - a legitimate part of the scenario
        _fid, payload, x2 = m.groups()
        x = 0
        for ch in payload:
            x ^= ord(ch)
        assert f"{x:02x}" == x2, f"bad checksum in the stimulus: {raw!r}"
        checked += 1
    assert checked >= 4


def test_wait_serial_requires_preceding_trigger(tmp_path):
    # IH-14: a wait-serial answer is anchored to the stimulus that asked for it;
    # a wait without a preceding write-serial/mqtt-publish would false-flag
    # honest boot output as pre-printed cheating - rejected at load time.
    d = tmp_path / "t"
    d.mkdir()
    (d / "task.yaml").write_text(
        textwrap.dedent(
            """
        name: bad-order
        description: fake
        entry: solution.py
        target: unix
        timeout_sec: 5
        expect:
          - 'boot'
        stimulus:
          - wait-serial: "boot"
        """
        ),
        encoding="utf-8",
    )
    (d / "solution.py").write_text("print('boot')\n", encoding="utf-8")
    with pytest.raises(ValueError, match="wait-serial must be preceded"):
        load_task(d)


def test_timeout_sec_is_bounded(tmp_path):
    """IH-46: numeric caps on operator-authored fields - a billion-second
    timeout would make the wall deadline meaningless."""
    d = write_task(tmp_path, "name: t\nexpect:\n  - 'x'\ntimeout_sec: 1000000000\n")
    with pytest.raises(ValueError, match="timeout_sec"):
        load_task(d)


def test_plant_loop_length_is_bounded(tmp_path):
    """IH-46: duration/dt defines the closed-loop step count - an unbounded
    ratio means an unbounded run. 10^6 s at dt=0.05 is 2*10^7 steps."""
    d = write_task(
        tmp_path,
        "name: p\ntarget: plant\nplant:\n  K: 1\n  T: 1\n  duration: 1000000\n"
        "  setpoint: 50\n  dt: 0.05\n  requirements: {steady_error: 1.0}\n",
    )
    with pytest.raises(ValueError, match="duration/dt"):
        load_task(d)


def test_mqtt_collect_timeout_is_bounded(tmp_path):
    """IH-46 review F2: mqtt-collect.timeout_sec derives a runner deadline -
    a billion-second collect would defeat the wall deadline."""
    d = write_task(
        tmp_path,
        "name: m\ntarget: unix\nmqtt: {client_id: c}\ntimeout_sec: 5\n"
        "stimulus:\n  - mqtt-collect: {topic: t, count: 1, timeout_sec: 1000000000}\n",
    )
    with pytest.raises(ValueError, match="mqtt-collect"):
        load_task(d)


def test_mqtt_collect_nan_timeout_is_rejected(tmp_path):
    """IH-46 review: YAML .nan passes both 0 < nan and nan > 600 checks -
    a non-finite collect timeout must be rejected like an unbounded one."""
    d = write_task(
        tmp_path,
        "name: m\ntarget: unix\nmqtt: {client_id: c}\ntimeout_sec: 5\n"
        "stimulus:\n  - mqtt-collect: {topic: t, count: 1, timeout_sec: .nan}\n",
    )
    with pytest.raises(ValueError, match="mqtt-collect"):
        load_task(d)


def test_environment_device_modules_parsed(tmp_path):
    """IH-65: device_modules declares the board's available modules - they
    surface into the solve prompt (agents assumed a pip ecosystem and wrote
    `import bme280` on a bare board)."""
    d = write_task(
        tmp_path,
        "name: envt\ntimeout_sec: 5\n"
        "environment:\n  device_modules: [machine, time, struct]\n"
        "expect:\n  - 'x'\n",
    )
    task = load_task(d)
    assert task.device_modules == ("machine", "time", "struct")


def test_environment_device_modules_reject_duplicates_and_non_strings(tmp_path):
    d = write_task(
        tmp_path,
        "name: envd\ntimeout_sec: 5\n"
        "environment:\n  device_modules: [time, time]\n"
        "expect:\n  - 'x'\n",
    )
    with pytest.raises(ValueError, match="duplicates"):
        load_task(d)
    d2 = write_task(
        tmp_path,
        'name: envn\ntimeout_sec: 5\n'
        'environment:\n  device_modules: ["", time]\n'
        "expect:\n  - 'x'\n",
        dirname="envn",
    )
    with pytest.raises(ValueError, match="non-empty"):
        load_task(d2)


def test_first_prompt_surfaces_device_modules():
    """IH-65: the environment fact must be in the BASE prompt - both A/B
    arms get it (stripping it with the notes would make the bare arm fight
    an undeclared environment)."""
    from ironbench.agent import _first_prompt
    from ironbench.tasks import Task

    task = Task(
        name="t",
        description="d",
        directory=Path("."),
        scenario=None,
        entry="main.py",
        timeout_sec=5,
        expect=("x",),
        fail=(),
        device_modules=("machine", "time", "struct"),
    )
    prompt = _first_prompt(task)
    assert "NO third-party libraries" in prompt
    assert "machine, time, struct" in prompt


def test_plant_nan_tolerance_and_disturbance_are_rejected(tmp_path):
    """IH-46 review: a non-finite requirement tolerance makes every
    comparison False (the detector passes anything) and a non-finite
    disturbance ambient poisons y so all requirements auto-pass."""
    base = "name: n\ntarget: plant\nplant:\n  K: 1\n  T: 1\n  duration: 100\n  setpoint: 50\n"
    d = write_task(
        tmp_path,
        base + "  dt: 0.5\n  requirements: {steady_error: .inf}\n",
    )
    with pytest.raises(ValueError, match="finite"):
        load_task(d)
    d2 = write_task(
        tmp_path,
        base + "  dt: 0.5\n  requirements: {steady_error: 1.0}\n"
        "  disturbances:\n    - {at: 5, ambient: .nan}\n",
        dirname="n2",
    )
    with pytest.raises(ValueError, match="finite"):
        load_task(d2)


def test_plant_nan_numerics_are_rejected(tmp_path):
    """IH-46 review N4: YAML .nan floats bypass >-style bounds - reject them
    at load with an authoring error instead of crashing the runner."""
    d = write_task(
        tmp_path,
        "name: n\ntarget: plant\nplant:\n  K: 1\n  T: 1\n  duration: 100\n"
        "  setpoint: 50\n  dt: .nan\n  requirements: {steady_error: 1.0}\n",
    )
    with pytest.raises(ValueError, match="finite"):
        load_task(d)


def test_boot_expect_must_be_expect_members(tmp_path):
    # audit A: boot_expect drives the wokwi pre-printed exemption - a
    # literal outside expect would silently exempt unscoreable output
    d = write_task(
        tmp_path,
        "name: n\nexpect:\n  - 'ready'\nboot_expect:\n  - 'other'\n",
        dirname="bad-boot",
    )
    with pytest.raises(ValueError, match="boot_expect .* must be members of expect"):
        load_task(d)
    ok = write_task(
        tmp_path,
        "name: n\nexpect:\n  - 'ready'\nboot_expect:\n  - 'ready'\n",
        dirname="good-boot",
    )
    assert load_task(ok).boot_expect == ("ready",)
