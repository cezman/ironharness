"""Tests of loading ironbench tasks from YAML."""

from __future__ import annotations

import textwrap

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
    d = write_task(tmp_path, "name: t1\n")
    task = load_task(d)
    assert task.timeout_sec == 30
    assert task.scenario is None  # no scenario -> the runner generates REPL-paste
    assert task.entry == "main.py"
    assert task.expect == ()
    assert task.stimulus == ()


def test_load_task_stimulus(tmp_path):
    content = "name: t1\nstimulus:\n  - delay: 500ms\n  - write-serial: \"hi\\n\"\n"
    task = load_task(write_task(tmp_path, content))
    assert task.stimulus == ({"delay": "500ms"}, {"write-serial": "hi\n"})


def test_load_task_stimulus_must_be_steps(tmp_path):
    d = write_task(tmp_path, "name: t1\nstimulus:\n  - just a string\n")
    with pytest.raises(ValueError, match="stimulus"):
        load_task(d)


def test_load_task_stimulus_unknown_step_key(tmp_path):
    d = write_task(tmp_path, "name: t1\nstimulus:\n  - write-seral: 'x'\n")
    with pytest.raises(ValueError, match="write-seral"):
        load_task(d)


def test_load_task_requires_name(tmp_path):
    d = write_task(tmp_path, "timeout_sec: 5\n")
    with pytest.raises(ValueError, match="name"):
        load_task(d)


def test_load_task_expect_must_be_strings(tmp_path):
    d = write_task(tmp_path, "name: t1\nexpect:\n  - 42\n")
    with pytest.raises(ValueError, match="expect"):
        load_task(d)


def test_load_tasks_sorted_and_skips_dirs_without_task(tmp_path):
    write_task(tmp_path, "name: bbb\n", dirname="bbb")
    write_task(tmp_path, "name: aaa\n", dirname="aaa")
    (tmp_path / "empty_dir").mkdir()
    tasks = load_tasks(tmp_path)
    assert [t.name for t in tasks] == ["aaa", "bbb"]


def test_load_tasks_missing_dir(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        load_tasks(tmp_path / "nope")


def test_load_task_tags_and_level(tmp_path):
    write_task(tmp_path, "name: aaa\ntags: [io, fsm]\nlevel: 3\n", dirname="aaa")
    task = load_task(tmp_path / "aaa")
    assert task.tags == ("io", "fsm")
    assert task.level == 3


def test_load_task_rejects_unknown_tag(tmp_path):
    write_task(tmp_path, "name: aaa\ntags: [robots]\n", dirname="aaa")
    with pytest.raises(ValueError, match="unknown tags"):
        load_task(tmp_path / "aaa")


def test_load_task_rejects_duplicate_tags(tmp_path):
    write_task(tmp_path, "name: aaa\ntags: [io, io]\n", dirname="aaa")
    with pytest.raises(ValueError, match="duplicates"):
        load_task(tmp_path / "aaa")


def test_load_task_rejects_bad_level(tmp_path):
    write_task(tmp_path, "name: aaa\nlevel: 9\n", dirname="aaa")
    with pytest.raises(ValueError, match="level"):
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
    } <= names


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
