"""Tests of the machine shim and the events scoring (IH-16).

The shim models GPIO pins on the unix target: every transition is logged as
"PIN <id> <value> <t_ms>", and the runner's events section scores the count
and the timing of those transitions. Integration tests run a synthetic
firmware with a local python (no WSL); the real golden blink-unix is covered
WSL-guarded like the other golden unix tasks.
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest

from ironbench.runner import run_task
from ironbench.tasks import load_task

SHIM = Path(__file__).parents[1] / "src" / "ironbench" / "shims" / "machine.py"
TASKS_DIR = Path(__file__).parents[1] / "src" / "ironbench" / "tasks"


def load_shim():
    spec = importlib.util.spec_from_file_location("machine_under_test", SHIM)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_shim_logs_transitions_with_timestamps(capsys):
    shim = load_shim()
    pin = shim.Pin(2, shim.Pin.OUT)
    pin.value(1)
    pin.off()
    pin.toggle()
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 3
    stamps = []
    for line, expect_state in zip(out, ("1", "0", "1")):
        parts = line.split()
        assert parts[0] == "PIN" and parts[1] == "2" and parts[2] == expect_state
        stamps.append(float(parts[3]))
    assert stamps == sorted(stamps)  # monotone timestamps


def test_shim_value_read_and_no_change_no_log(capsys):
    shim = load_shim()
    pin = shim.Pin(2, shim.Pin.OUT, value=1)
    assert pin.value() == 1
    pin.value(1)  # no change - no event
    assert capsys.readouterr().out == ""


def make_shim_task(
    tmp_path,
    entry_code: str,
    *,
    period_ms=(30, 120),
    count_min=8,
    expect=("boot",),
):
    d = tmp_path / "t"
    d.mkdir()
    lo, hi = period_ms
    expect_lines = [f"  - {p!r}" for p in expect]
    lines = [
        "name: synth-shim",
        "description: fake",
        "entry: solution.py",
        "target: unix",
        "shim: machine",
        "timeout_sec: 10",
        "expect:",
        *expect_lines,
        "events:",
        "  - pattern: '^PIN 2 [01] '",
        f"    count_min: {count_min}",
        f"    period_ms: [{lo}, {hi}]",
    ]
    (d / "task.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (d / "solution.py").write_text(entry_code, encoding="utf-8")
    return load_task(d)


def run_local(tmp_path, task):
    return run_task(
        task, out_dir=tmp_path / "out", unix_cmd=[sys.executable, str(task.directory / "solution.py")]
    )


def test_shim_staged_next_to_entry_and_good_timing_passes(tmp_path):
    entry = (
        "from machine import Pin\n"
        "import time\n"
        "print('boot', flush=True)\n"
        "pin = Pin(2, Pin.OUT)\n"
        "for _ in range(8):\n"
        "    pin.toggle()\n"
        "    time.sleep(0.06)\n"
    )
    task = make_shim_task(tmp_path, entry, expect=())  # no plain expects: run to EOF
    res = run_local(tmp_path, task)
    assert res.passed, (res.error, res.missed)
    assert (task.directory / "machine.py").is_file()  # the shim was staged


def test_wrong_period_fails_events(tmp_path):
    entry = (
        "from machine import Pin\n"
        "import time\n"
        "print('boot', flush=True)\n"
        "pin = Pin(2, Pin.OUT)\n"
        "for _ in range(8):\n"
        "    pin.toggle()\n"
        "    time.sleep(0.3)\n"  # 300 ms: outside the declared 30..120 ms band
    )
    task = make_shim_task(tmp_path, entry, expect=())  # run to EOF for all events
    res = run_local(tmp_path, task)
    assert not res.passed
    assert any("period" in m for m in res.missed)


def test_bare_print_cheater_fails_event_count(tmp_path):
    # The verbatim cheat: prints the expected strings without any GPIO events -
    # no parseable timestamps, so the events count fails.
    entry = "print('boot')\nprint('PIN 2 1')\nprint('PIN 2 0')\n"
    task = make_shim_task(tmp_path, entry)
    res = run_local(tmp_path, task)
    assert not res.passed
    assert any("events" in m for m in res.missed)


def test_shim_and_events_validation(tmp_path):
    header = textwrap.dedent(
        """
        name: v
        description: fake
        entry: solution.py
        timeout_sec: 5
        expect: []
        """
    )
    cases = [
        ("shim: machine\n", "only supported by the unix target"),  # no target -> wokwi
        (
            "events:\n  - pattern: 'x'\n    count_min: 2\n",
            "events section is only supported by the unix target",
        ),
        ("target: unix\nshim: gpio\n", "unknown shim"),
        ("target: unix\nevents:\n  - count_min: 2\n", "events.pattern"),
        (
            "target: unix\nevents:\n  - pattern: 'x'\n    count_min: 0\n",
            "count_min must be an integer >= 1",
        ),
        (
            "target: unix\nevents:\n  - pattern: 'x'\n    count_min: 2\n    period_ms: [5, 5]\n",
            "0 < lo < hi",
        ),
        (
            "target: unix\nexpect:\n  - '([unterminated'\nevents:\n  - pattern: 'x'\n    count_min: 1\n",
            "invalid regex",
        ),
        (
            "target: unix\nevents:\n  - pattern: '([bad'\n    count_min: 1\n",
            "invalid regex",
        ),
    ]
    for extra, expected in cases:
        d = tmp_path / "t"
        d.mkdir(exist_ok=True)
        (d / "task.yaml").write_text(header + textwrap.dedent(extra), encoding="utf-8")
        (d / "solution.py").write_text("print('x')\n", encoding="utf-8")
        with pytest.raises(ValueError, match=expected):
            load_task(d)


def test_empty_events_section_parses_as_empty(tmp_path):
    # 'events:' with no items is an empty section (the `or []` convention),
    # not a confusing 'must be a list' error.
    d = tmp_path / "t"
    d.mkdir()
    (d / "task.yaml").write_text(
        textwrap.dedent(
            """
        name: v
        description: fake
        entry: solution.py
        target: unix
        timeout_sec: 5
        expect:
          - 'x'
        events:
        """
        ),
        encoding="utf-8",
    )
    task = load_task(d)
    assert task.events == ()


def test_multiple_event_specs_are_both_enforced(tmp_path):
    entry = (
        "from machine import Pin\n"
        "import time\n"
        "pin = Pin(2, Pin.OUT)\n"
        "for _ in range(8):\n"
        "    pin.toggle()\n"
        "    time.sleep(0.06)\n"
    )
    d = tmp_path / "t"
    d.mkdir()
    lines = [
        "name: multi-spec",
        "description: fake",
        "entry: solution.py",
        "target: unix",
        "shim: machine",
        "timeout_sec: 10",
        "expect: []",
        "events:",
        "  - pattern: '^PIN 2 [01] '",  # satisfied: 8 events in period
        "    count_min: 8",
        "    period_ms: [30, 120]",
        "  - pattern: '^PIN 2 [01] '",  # violated: demands twice as many
        "    count_min: 16",
        "    period_ms: [30, 120]",
    ]
    (d / "task.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (d / "solution.py").write_text(entry, encoding="utf-8")
    task = load_task(d)
    res = run_local(tmp_path, task)
    assert not res.passed
    assert sum("16" in m for m in res.missed) == 1  # only the second spec failed


def test_non_numeric_tail_is_not_an_event(tmp_path):
    # A matched line without a parseable trailing timestamp is not an event:
    # printing the expected strings plus junk cannot satisfy the count.
    entry = (
        "print('boot')\n"
        "for _ in range(8):\n"
        "    print('PIN 2 1 X')\n"  # the pattern matches, the tail is junk
        "    print('PIN 2 0 Y')\n"
    )
    task = make_shim_task(tmp_path, entry, expect=())
    res = run_local(tmp_path, task)
    assert not res.passed
    assert any("events" in m for m in res.missed)


def test_golden_blink_unix_passes(tmp_path, wsl_unix_ready):
    if not wsl_unix_ready:
        pytest.skip("needs a WSL distro with micropython")
    task = load_task(TASKS_DIR / "blink-unix")
    res = run_task(task, out_dir=tmp_path / "out")
    assert res.passed, (res.error, res.missed)
