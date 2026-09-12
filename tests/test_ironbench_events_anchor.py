"""IH-24 tests: the event-line anchor to chunk ingestion stamps and the
detector validation. A cheater that simulates the shim's event format with
well-timed FAKE timestamps (one dump, embedded t_ms spaced 500 ms) must
fail the real-time anchor - the dump is ingested within milliseconds, so
the REAL intervals violate the declared period. Honest firmware paced by
real sleeps passes.
"""

from __future__ import annotations

import textwrap

from ironbench.runner import run_task
from ironbench.tasks import load_task


def make_events_task(tmp_path, entry_code: str):
    d = tmp_path / "t"
    d.mkdir()
    text = textwrap.dedent(
        """
    name: fake-events
    description: fake
    entry: solution.py
    target: unix
    timeout_sec: 10
    expect: []
    events:
      - pattern: '^PIN 2 [01] '
        count_min: 4
        period_ms: [350, 650]
    stimulus:
      - delay: 200ms
      - write-serial: "go\\r"
    """
    )
    (d / "task.yaml").write_text(text, encoding="utf-8")
    (d / "solution.py").write_text(entry_code, encoding="utf-8")
    return load_task(d)


def run_events(tmp_path, code: str):
    import sys

    task = make_events_task(tmp_path, code)
    cmd = [sys.executable, str(task.directory / "solution.py")]
    return run_task(task, out_dir=tmp_path / "out", unix_cmd=cmd)


def test_fake_timestamp_dump_fails_realtime_anchor(tmp_path):
    # The IH-16-era cheater, exactly: prints the shim's event lines with
    # embedded fake timestamps spaced 500 ms - the old text-only scoring
    # passes both the count and the period bounds. The IH-24 anchor fails
    # it: one dump is ingested within milliseconds.
    fake = (
        "import sys\n"
        "line = sys.stdin.readline()  # wait for the trigger\n"
        "for i in range(4):\n"
        "    print(f'PIN 2 {i % 2} {i * 500}')\n"
    )
    res = run_events(tmp_path, fake)
    assert not res.passed, (res.error, res.missed)
    assert any("real-time" in m for m in res.missed), res.missed


def test_real_paced_events_pass_the_anchor(tmp_path):
    # honest firmware: 4 toggles paced by real sleeps (0.5 s) - the real
    # ingestion gaps match the declared period within the tolerance. flush
    # mirrors MicroPython, which emits every line as it prints (a CPython
    # pipe would buffer the whole run until exit and defeat the anchor).
    honest = (
        "import sys, time\n"
        "line = sys.stdin.readline()\n"
        "for i in range(4):\n"
        "    print(f'PIN 2 {i % 2} {i * 500}', flush=True)\n"
        "    time.sleep(0.5)\n"
    )
    res = run_events(tmp_path, honest)
    assert res.passed, (res.error, res.missed)


def test_passive_events_task_keeps_text_only_scoring(tmp_path):
    # no stimulus triggers: the anchor legitimately does not apply (there is
    # no stimulus to anchor to) - the documented residual stays for passives
    d = tmp_path / "passive"
    d.mkdir()
    text = textwrap.dedent(
        """
    name: passive-events
    description: fake
    entry: solution.py
    target: unix
    timeout_sec: 8
    expect: []
    events:
      - pattern: '^PIN 2 [01] '
        count_min: 4
        period_ms: [350, 650]
    """
    )
    (d / "task.yaml").write_text(text, encoding="utf-8")
    fake = "for i in range(4):\n    print(f'PIN 2 {i % 2} {i * 500}')\n"
    (d / "solution.py").write_text(fake, encoding="utf-8")
    task = load_task(d)
    import sys

    res = run_task(task, out_dir=tmp_path / "out", unix_cmd=[sys.executable, str(d / "solution.py")])
    # the fake-timestamp dump PASSES: no anchor is possible without a trigger
    assert res.passed, (res.error, res.missed)


def test_task_without_detector_is_rejected(tmp_path):
    # IH-24 validation: neither expect nor events - nothing to score, any
    # output would pass. Load must refuse.
    d = tmp_path / "no-detector"
    d.mkdir()
    (d / "task.yaml").write_text(
        "name: empty\ndescription: fake\nentry: main.py\ntarget: unix\ntimeout_sec: 5\n",
        encoding="utf-8",
    )
    (d / "main.py").write_text("print('x')\n", encoding="utf-8")
    import pytest

    with pytest.raises(ValueError, match="no scoring detector"):
        load_task(d)


def test_empty_expect_without_events_is_rejected(tmp_path):
    # expect: [] with no events section - the same vacuous hole
    d = tmp_path / "empty-expect"
    d.mkdir()
    (d / "task.yaml").write_text(
        "name: empty-expect\ndescription: fake\nentry: main.py\ntarget: unix\n"
        "timeout_sec: 5\nexpect: []\n",
        encoding="utf-8",
    )
    (d / "main.py").write_text("print('x')\n", encoding="utf-8")
    import pytest

    with pytest.raises(ValueError, match="no scoring detector"):
        load_task(d)


def test_blink_unix_loads_with_empty_expect(tmp_path):
    # the legit case: expect: [] + an events section is a valid detector pair
    from pathlib import Path

    task = load_task(Path(__file__).parents[1] / "src" / "ironbench" / "tasks" / "blink-unix")
    assert task.expect == ()
    assert task.events
